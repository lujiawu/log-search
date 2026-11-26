"""Linux child processes and system OpenSSH transport."""

import asyncio
import hashlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import ssh_config_path
from . import worker


class AuthenticationRequired(RuntimeError):
    pass


def validate_target(target):
    if not isinstance(target, str) or not target or target.startswith("-") or re.search(r"[\s\x00-\x1f]", target):
        raise ValueError("非法 SSH 目标")


async def terminate(process):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), 1)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


class SshTransport:
    def __init__(self, config=None, *, operations=None):
        self.config = ssh_config_path(config)
        self.directory = None
        self.masters = {}
        self.lock = asyncio.Lock()
        self.operations = operations

    def log(self, event, **fields):
        if self.operations:
            self.operations.emit(event, ssh_config=self.config, **fields)

    def arguments(self, target):
        validate_target(target)
        if not shutil.which("ssh"):
            raise ValueError("本机缺少 OpenSSH 客户端")
        return ["ssh", "-F", self.config, "-T", "-o", "RemoteCommand=none", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=10"]

    def socket(self, target):
        validate_target(target)
        if self.directory is None:
            self.directory = tempfile.TemporaryDirectory(prefix="log-search-ssh-")
        if target not in self.masters:
            self.masters[target] = str(Path(self.directory.name) / ("s" + str(len(self.masters))))
        return self.masters[target]

    async def display_target(self, target):
        """Resolve the display through OpenSSH without opening a connection."""
        process = await asyncio.create_subprocess_exec(*self.arguments(target), "-G", target, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 3)
            values = {}
            for line in output.decode("utf-8", "replace").splitlines():
                key, _, value = line.partition(" ")
                if key in ("user", "hostname"):
                    values[key] = value.strip()
            if process.returncode or not values.get("user") or not values.get("hostname"):
                raise ValueError("无法解析 SSH 显示目标")
            return values["user"] + "@" + values["hostname"]
        finally:
            await terminate(process)

    def master_command(self, target, interactive=False):
        return self.arguments(target) + ["-M", "-N", "-f", "-o", "ControlMaster=yes", "-o", "ControlPersist=60", "-o", "ControlPath=" + self.socket(target), "-o", "BatchMode=" + ("no" if interactive else "yes"), target]

    def authenticate(self, target):
        """Run while the caller has suspended the TUI and restored its terminal."""
        command = self.master_command(target, interactive=True)
        self.log("ssh.authentication_started", target=target, interactive=True)
        # Diagnostics and password/host prompts use the real terminal, not JSON stdout.
        try:
            with open("/dev/tty", "r+b", buffering=0) as terminal:
                result = subprocess.run(command, stdin=terminal, stdout=terminal, stderr=terminal)
        except OSError as exc:
            self.log("ssh.authentication_failed", level="error", target=target, message=str(exc))
            raise
        self.log("ssh.authentication_finished", target=target, returncode=result.returncode)
        if result.returncode:
            raise AuthenticationRequired("SSH 认证未完成")

    async def ensure_master(self, target, task_id=None):
        async with self.lock:
            self.log("ssh.connection_check", target=target, task_id=task_id)
            socket = self.socket(target)
            check = self.arguments(target) + ["-o", "ControlPath=" + socket, "-O", "check", target]
            process = await asyncio.create_subprocess_exec(*check, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:
                checked = await asyncio.wait_for(process.wait(), 3)
            finally:
                await terminate(process)
            if checked == 0:
                self.log("ssh.connection_reused", target=target, task_id=task_id)
                return
            self.log("ssh.connection_started", target=target, task_id=task_id, interactive=False)
            process = await asyncio.create_subprocess_exec(*self.master_command(target), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
            try:
                _, error = await asyncio.wait_for(process.communicate(), 15)
                self.log("ssh.connection_finished", level="warning" if process.returncode else "info", target=target, task_id=task_id, returncode=process.returncode, stderr=error.decode("utf-8", "replace")[:4096])
                if process.returncode:
                    raise AuthenticationRequired("SSH 连接失败；如需密码或主机确认，请先认证。" + worker.redact(error.decode("utf-8", "replace")[:4096]))
            finally:
                await terminate(process)

    async def open(self, target, *, task_id=None):
        await self.ensure_master(target, task_id)
        # Use the same buffered stdin object for the source frame and request stream.
        bootstrap = "import sys; n=int(sys.stdin.buffer.readline(32)); assert 0<n<1048576; s=sys.stdin.buffer.read(n); assert len(s)==n; exec(compile(s,'<log-search-worker>','exec'),{'__name__':'__main__'})"
        remote = "python3 -u -c " + shlex.quote(bootstrap)
        command = self.arguments(target) + ["-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPath=" + self.socket(target), target, remote]
        process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=worker.MAX_MESSAGE + 1, start_new_session=True)
        source = Path(worker.__file__).read_bytes()
        process.stdin.write(str(len(source)).encode("ascii") + b"\n" + source)
        try:
            await asyncio.wait_for(process.stdin.drain(), 5)
            self.log("ssh.helper_sent", target=target, task_id=task_id, process_pid=process.pid, source_bytes=len(source), source_sha256=hashlib.sha256(source).hexdigest(), helper_version=worker.VERSION)
        except BaseException:
            self.log("ssh.helper_send_failed", level="error", target=target, task_id=task_id)
            await terminate(process)
            raise
        return process

    async def close(self):
        for target, socket in self.masters.items():
            self.log("ssh.connection_closing", target=target)
            command = self.arguments(target) + ["-o", "ControlPath=" + socket, "-O", "exit", target]
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                self.log("ssh.connection_close_timeout", level="warning", target=target)
            finally:
                await terminate(process)
                self.log("ssh.connection_closed", target=target, returncode=process.returncode)
        self.masters.clear()
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None


async def local_worker():
    return await asyncio.create_subprocess_exec(sys.executable, "-u", str(Path(worker.__file__)), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=worker.MAX_MESSAGE + 1, start_new_session=True)

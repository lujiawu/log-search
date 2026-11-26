"""Real OpenSSH transport against an isolated, ephemeral loopback sshd."""

import asyncio
import gzip
import os
import pwd
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from log_search.controller import Controller, request


@pytest.fixture
def loopback(tmp_path):
    sshd = shutil.which("sshd")
    if not sshd or not shutil.which("ssh-keygen"):
        pytest.skip("sshd / ssh-keygen unavailable")
    for name in ("host", "identity"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / name)], check=True, capture_output=True)
    (tmp_path / "authorized").write_text((tmp_path / "identity.pub").read_text())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    user = pwd.getpwuid(os.getuid()).pw_name
    config = tmp_path / "sshd_config"
    config.write_text("\n".join([f"Port {port}", "ListenAddress 127.0.0.1", "UsePAM no", "PasswordAuthentication no", "KbdInteractiveAuthentication no", "PubkeyAuthentication yes", "StrictModes no", "PermitRootLogin yes", f"HostKey {tmp_path / 'host'}", f"AuthorizedKeysFile {tmp_path / 'authorized'}", f"PidFile {tmp_path / 'sshd.pid'}"]) + "\n")
    log = (tmp_path / "sshd.log").open("w+")
    server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stdout=subprocess.DEVNULL, stderr=log)
    key = (tmp_path / "host.pub").read_text().split()
    (tmp_path / "known_hosts").write_text(f"[127.0.0.1]:{port} {key[0]} {key[1]}\n")
    client = tmp_path / "ssh_config"
    client.write_text("\n".join(["Host fixture", "  HostName 127.0.0.1", f"  User {user}", f"  Port {port}", f"  IdentityFile {tmp_path / 'identity'}", "  IdentitiesOnly yes", f"  UserKnownHostsFile {tmp_path / 'known_hosts'}", "  StrictHostKeyChecking yes", "  RequestTTY force", "  RemoteCommand echo SHOULD_NOT_RUN"]) + "\n")
    try:
        yield server, client, log
    finally:
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=3)
        log.close()


async def test_actual_ssh_source_query_context_and_cancel(loopback, tmp_path):
    server, config, log = loopback
    await asyncio.sleep(.15)
    if server.poll() is not None:
        log.seek(0)
        pytest.skip("local sshd cannot start: " + log.read()[:500])
    root = tmp_path / "deployment with spaces"
    logs = root / "app-alpha/logs/history"
    logs.mkdir(parents=True)
    (logs / "info.1").write_bytes(b"2026-01-01 10:00:00 ERROR TRACE\n\x00 at demo\n")
    controller = Controller(str(config))
    try:
        task = await controller.start(request(root=str(root), query="TRACE"), "fixture")
        events = [event async for event in task.events()]
        assert task.done["scope_complete"], events
        assert len(task.entries) == 1
        entry = task.entries[0]
        value = request("context", root=str(root), file=entry["file"], line=entry["start_line"], source_identity=entry["source_identity"])
        result, done, warnings = await controller.context(value, "fixture")
        assert result and done["scope_complete"], warnings
        with gzip.open(logs / "large.gz", "wb", compresslevel=1) as output:
            block = b"2026-01-01 10:00:00 INFO no match\n" * 20000
            for _ in range(50):
                output.write(block)
        task = await controller.start(request(root=str(root), query="TRACE"), "fixture")
        worker_pid = None
        async for event in task.events():
            if event["type"] == "ready":
                worker_pid = event["worker_pid"]
                await task.cancel()
        assert task.done["outcome"] == "cancelled"
        assert not task.done["scope_complete"]
        assert worker_pid is not None
        for _ in range(20):
            if not Path(f"/proc/{worker_pid}").exists():
                break
            await asyncio.sleep(.05)
        assert not Path(f"/proc/{worker_pid}").exists()
    finally:
        await controller.close()
    assert not controller.ssh.masters and controller.ssh.directory is None

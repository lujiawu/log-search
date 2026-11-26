"""UI-independent task API shared by CLI and Textual."""

import asyncio
import contextlib
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import worker
from .operations import OperationLog
from .transport import SshTransport, local_worker, terminate, validate_target


def sort_key(entry):
    return (entry.get("ts") is None, entry.get("ts") or "", entry.get("service", ""), entry.get("file", ""), entry.get("start_line", 0))


def today_time_range(now=None):
    start = (now or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%d %H:%M:%S"), (start + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


def request(action="search", *, host=None, root=None, log_dirs=None, query="", since=None, until=None, include_archives=True, include_unknown_time=False, timeout=60, max_entries=100, **extra):
    scope = {key: value for key, value in (("root", root), ("log_dirs", log_dirs)) if value}
    value = dict(protocol=1, task_id=uuid.uuid4().hex, request_id=uuid.uuid4().hex, action=action, scope=scope, query=query, since=since, until=until, include_archives=include_archives, include_unknown_time=include_unknown_time, limits=dict(timeout_seconds=timeout, max_entries=max_entries, max_bytes=32 * 1024 * 1024), **extra)
    worker.validate(value)
    if host:
        validate_target(host)
    return value


class Task:
    def __init__(self, controller, value, host):
        self.controller = controller
        self.request = json.loads(json.dumps(value))
        self.host = host
        self.queue = asyncio.Queue(maxsize=64)
        self.entries = []
        self.done = None
        self.process = None
        self.cancelled = False
        self.started_at = time.time()
        self.finished_at = None
        self.warnings = []
        self.diagnostics = ""
        self.payload_bytes = 0
        self.runner_started = False
        self.runner = asyncio.create_task(self._run())

    def log(self, event, **fields):
        self.controller.operations.emit(event, **dict(fields, task_id=self.request["task_id"], request_id=self.request["request_id"]))

    async def _publish(self, event):
        if event["type"] == "entry":
            self.entries.append(event["entry"])
            entry = event["entry"]
            self.log("task.entry", seq=event.get("seq"), entry={key: entry.get(key) for key in ("id", "ts", "service", "level", "file", "start_line", "end_line", "preview_truncated", "source_changed", "capture_complete", "raw_bytes", "content_kind")}, received_entries=len(self.entries))
        elif event["type"] == "done":
            self.done = event
            self.finished_at = time.time()
            self.log("task.done", **{key: value for key, value in event.items() if key not in ("type", "task_id", "request_id")}, received_entries=len(self.entries), payload_bytes=self.payload_bytes)
        elif event["type"] == "warning":
            if event.get("code") == "source_changed":
                root = self.request["scope"].get("root", "")
                for entry in self.entries:
                    if str(Path(root) / entry["file"]) == event.get("file"):
                        entry["source_changed"] = True
            if len(self.warnings) < 100:
                self.warnings.append(event)
            self.log("task.warning", level="warning", code=event.get("code"), message=event.get("message"), file=event.get("file"), seq=event.get("seq"))
        elif event["type"] in ("ready", "progress", "phase"):
            self.log("task." + event["type"], **{key: value for key, value in event.items() if key not in ("type", "task_id", "request_id")})
        elif event["type"] == "logs":
            self.log("task.logs", directories=event["directories"], directories_count=len(event["directories"]))
        elif event["type"] == "context":
            self.log("task.context", entries_count=len(event["entries"]), sources=[dict(file=entry["file"], line=entry["start_line"], preview_truncated=entry["preview_truncated"]) for entry in event["entries"]], truncated=event.get("truncated"), source_changed=event.get("source_changed"))
        await self.queue.put(event)

    async def _read_stderr(self):
        while True:
            block = await self.process.stderr.read(4096)
            if not block:
                return
            if len(self.diagnostics) < 8192:
                self.diagnostics += worker.redact(block.decode("utf-8", "replace"))[:8192 - len(self.diagnostics)]

    async def _run(self):
        self.runner_started = True
        stderr_task = None
        try:
            if self.cancelled:
                return
            self.log("task.connecting", target=self.host or "local", ssh_config=self.controller.ssh.config if self.host else None)
            self.process = await asyncio.wait_for(self.controller.ssh.open(self.host, task_id=self.request["task_id"]) if self.host else local_worker(), 25)
            self.log("task.worker_started", process_pid=self.process.pid, location="ssh" if self.host else "local")
            stderr_task = asyncio.create_task(self._read_stderr())
            if self.cancelled:
                return
            self.process.stdin.write((json.dumps(self.request) + "\n").encode())
            await self.process.stdin.drain()
            self.log("task.request_sent", request=self.request, target=self.host or "local")
            sequence = 0
            deadline = time.monotonic() + self.request.get("limits", {}).get("timeout_seconds", 60) + 5
            while True:
                raw = await asyncio.wait_for(self.process.stdout.readline(), max(.01, deadline - time.monotonic()))
                if not raw:
                    break
                if len(raw) > worker.MAX_MESSAGE:
                    raise ValueError("远端协议消息超限")
                event = json.loads(raw)
                if event.get("protocol") != 1 or event.get("task_id") != self.request["task_id"] or event.get("request_id") != self.request["request_id"] or event.get("seq") != sequence + 1:
                    raise ValueError("worker 协议或事件顺序不匹配")
                sequence += 1
                if event.get("type") not in ("ready", "progress", "phase", "logs", "warning", "entry", "context", "done"):
                    raise ValueError("未知 worker 事件")
                if event["type"] == "entry":
                    self.payload_bytes += len(raw)
                    if len(self.entries) >= self.request["limits"]["max_entries"] or self.payload_bytes > self.request["limits"]["max_bytes"]:
                        await self._publish(dict(type="warning", code="local_limit", message="本地结果存储达到限额"))
                        await self._stop_worker()
                        await self._publish(dict(type="done", outcome="limited", scope_complete=False, content_complete=False, entries=len(self.entries)))
                        break
                await self._publish(event)
                if event["type"] == "done":
                    break
            if self.done is None and not self.cancelled:
                await asyncio.sleep(0)
                raise RuntimeError("worker 未发送终态，结果不完整。" + self.diagnostics)
        except asyncio.CancelledError:
            self.cancelled = True
            self.log("task.runner_cancelled")
        except asyncio.TimeoutError:
            self.log("task.response_timeout", level="warning")
            await self._publish(dict(type="warning", code="timeout", message="连接或 worker 响应超时"))
            if self.done is None:
                await self._publish(dict(type="done", outcome="timed_out", scope_complete=False, content_complete=False, entries=len(self.entries)))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            self.controller.operations.exception("task.exception", exc, task_id=self.request["task_id"], request_id=self.request["request_id"])
            await self._publish(dict(type="warning", code="transport", message=str(exc)))
        finally:
            if self.process:
                if self.process.stdin:
                    self.process.stdin.close()
                # stdin EOF gives a remote helper a chance to kill its rg children.
                try:
                    await asyncio.wait_for(self.process.wait(), 1)
                except asyncio.TimeoutError:
                    self.log("task.force_terminate", level="warning", process_pid=self.process.pid)
                    await terminate(self.process)
            if stderr_task:
                try:
                    await asyncio.wait_for(asyncio.shield(stderr_task), .5)
                except asyncio.TimeoutError:
                    stderr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stderr_task
            self.log("task.process_finished", process_pid=self.process.pid if self.process else None, returncode=self.process.returncode if self.process else None, stderr=self.diagnostics or None)
            if self.done is None:
                await self._publish(dict(type="done", outcome="cancelled" if self.cancelled else "failed", scope_complete=False, content_complete=False, entries=len(self.entries)))
            await self.queue.put(None)

    async def _stop_worker(self):
        if self.process and self.process.returncode is None:
            try:
                message = dict(action="cancel", task_id=self.request["task_id"])
                self.process.stdin.write((json.dumps(message) + "\n").encode())
                await asyncio.wait_for(self.process.stdin.drain(), .5)
            except (BrokenPipeError, ConnectionError, asyncio.TimeoutError):
                pass

    async def cancel(self):
        if self.done is not None:
            return
        self.log("task.cancel_requested", received_entries=len(self.entries))
        self.cancelled = True
        await self._stop_worker()
        if self.process:
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                await terminate(self.process)
        elif self.runner_started and not self.runner.done():
            self.runner.cancel()

    async def events(self):
        while True:
            if self.runner.done() and self.queue.empty():
                break
            try:
                event = await asyncio.wait_for(self.queue.get(), .1)
            except asyncio.TimeoutError:
                continue
            if event is None:
                break
            yield event


class Controller:
    def __init__(self, ssh_config=None, *, role="controller"):
        self.operations = OperationLog(role)
        self.ssh = SshTransport(ssh_config, operations=self.operations)
        self.tasks = set()

    async def start(self, value, host=None):
        try:
            worker.validate(value)
            if host:
                validate_target(host)
        except (ValueError, TypeError, KeyError) as exc:
            self.operations.exception("task.validation_failed", exc, request=value, target=host or "local")
            raise
        self.tasks = {task for task in self.tasks if not task.runner.done()}
        if value.get("action", "search") == "search" and any(task.request.get("action", "search") == "search" and task.done is None for task in self.tasks):
            self.operations.emit("task.rejected", level="warning", reason="search_already_running", request=value, target=host or "local")
            raise ValueError("已有搜索任务运行，请先取消")
        task = Task(self, value, host)
        self.tasks.add(task)
        task.log("task.created", target=host or "local", request=task.request)
        return task

    async def context(self, value, host=None):
        task = await self.start(value, host)
        result = None
        async for event in task.events():
            if event["type"] == "context":
                result = event
        return result, task.done, task.warnings

    async def logs(self, value, host=None):
        task = await self.start(value, host)
        result = None
        try:
            async for event in task.events():
                if event["type"] == "logs":
                    result = event["directories"]
        finally:
            await task.cancel()
        return result, task.done, task.warnings

    async def close(self):
        self.operations.emit("controller.closing", active_tasks=sum(task.done is None for task in self.tasks))
        for task in tuple(self.tasks):
            await task.cancel()
            # A UI may stop consuming events on close. Drain to release backpressure.
            while not task.runner.done():
                try:
                    await asyncio.wait_for(task.queue.get(), .1)
                except asyncio.TimeoutError:
                    pass
            await task.runner
        try:
            await self.ssh.close()
        except (OSError, RuntimeError) as exc:
            self.operations.exception("controller.cleanup_failed", exc)
            raise
        finally:
            self.operations.close()

"""Clipboard helpers are optional; never pass log content as command arguments."""

import asyncio
import contextlib
import os
import shutil


async def copy_text(app, text):
    operations = getattr(app, "operations", None)
    task = getattr(app, "search_task", None)

    def record(event, **fields):
        if operations:
            operations.emit(event, task_id=task.request["task_id"] if task else None, **fields)

    if not text:
        record("clipboard.empty")
        return "没有可复制的文字"
    record("clipboard.started", characters=len(text), bytes=len(text.encode("utf-8")))
    commands = []
    if os.environ.get("WAYLAND_DISPLAY"):
        commands.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        commands.extend((["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]))
    for command in commands:
        if not shutil.which(command[0]):
            continue
        process = None
        try:
            record("clipboard.backend_started", backend=command[0])
            process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(process.communicate(text.encode("utf-8")), 2)
            if process.returncode == 0:
                record("clipboard.finished", backend=command[0], result="confirmed")
                return "已复制到系统剪贴板"
            record("clipboard.backend_failed", level="warning", backend=command[0], returncode=process.returncode)
        except (OSError, asyncio.TimeoutError) as exc:
            record("clipboard.backend_failed", level="warning", backend=command[0], error_type=type(exc).__name__)
        finally:
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
    app.copy_to_clipboard(text)
    record("clipboard.finished", backend="osc52", result="requested_not_confirmed")
    return "已发送终端复制请求（OSC52）；若终端禁用，可用 CLI --export 落盘"

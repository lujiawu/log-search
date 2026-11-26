import asyncio
from datetime import datetime
import json
import stat
import subprocess
import sys

from textual.widgets import DataTable, Input

from log_search.controller import Controller, request
from log_search.operations import OperationLog, log_path, query_info, read_records
from log_search.tui import LogSearchApp

TRACE = "0c04b247-3706-4192-a405-3c808522fd00"


def make_logs(tmp_path):
    logs = tmp_path / "deployment/app-alpha/logs"
    logs.mkdir(parents=True)
    (logs / "rolling").write_text(datetime.now().strftime("%Y-%m-%d") + " 10:00:00 INFO " + TRACE + " BUSINESS_BODY_MUST_NOT_BE_LOGGED\n")
    return logs.parent.parent


def test_query_features_and_sensitive_fields(tmp_path):
    operations = OperationLog(path=tmp_path / "logs/operations.jsonl")
    operations.emit("test", query=" " + TRACE + " ", filters={"text": "private-literal"}, password="password-value", nested={"token": "token-value", "access_token": "access-value"}, body="BUSINESS_BODY", preview=["BUSINESS_PREVIEW"], clipboard="CLIPBOARD_CONTENT", message="Authorization: Bearer credential-value")
    operations.close()
    data = operations.path.read_text()
    for secret in ("private-literal", "password-value", "token-value", "access-value", "BUSINESS_BODY", "BUSINESS_PREVIEW", "CLIPBOARD_CONTENT", "credential-value"):
        assert secret not in data
    record = read_records(path=operations.path)[1]
    assert record["query"]["value"] == " " + TRACE + " "
    assert record["query"]["leading_whitespace"] == 1
    assert record["query"]["trailing_whitespace"] == 1
    assert query_info(TRACE)["sha256"] != query_info(" " + TRACE)["sha256"]
    assert stat.S_IMODE(operations.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(operations.path.parent.stat().st_mode) == 0o700


def test_rotation_and_corrupt_tail(tmp_path):
    operations = OperationLog(path=tmp_path / "logs/operations.jsonl", max_bytes=1200, backups=2)
    for index in range(30):
        operations.emit("test.progress", task_id="task", index=index)
    operations.close()
    files = [path for path in operations.path.parent.glob("operations.jsonl*") if path.suffix != ".lock"]
    assert len(files) == 3
    assert all(path.stat().st_size <= 1200 for path in files)
    records = read_records(path=operations.path, tail=3, task_id="task")
    assert [record["index"] for record in records] == [27, 28, 29]
    with operations.path.open("a") as output:
        output.write('{"partial":')
    assert read_records(path=operations.path, tail=1)[0]["event"] == "session.end"


def test_multiple_processes_share_valid_jsonl():
    code = "from log_search.operations import OperationLog; log=OperationLog('parallel'); [log.emit('parallel.event',index=i) for i in range(20)]; log.close()"
    processes = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            assert stdout == b""
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
    records = read_records(tail=1000)
    assert len(records) == 66
    assert len({record["session_id"] for record in records}) == 3
    assert sum(record["event"] == "parallel.event" for record in records) == 60


async def test_controller_audit_contains_trimmed_request_and_zero_outcome(tmp_path):
    root = make_logs(tmp_path)
    controller = Controller()
    try:
        task = await controller.start(request(root=str(root), query=TRACE))
        _ = [event async for event in task.events()]
        assert len(task.entries) == 1
        trimmed_match = await controller.start(request(root=str(root), query="  " + TRACE + " \t\n"))
        _ = [event async for event in trimmed_match.events()]
        assert trimmed_match.done["scope_complete"] and len(trimmed_match.entries) == 1
        failed_match = await controller.start(request(root=str(root), query=TRACE + "-missing"))
        _ = [event async for event in failed_match.events()]
        assert failed_match.done["scope_complete"] and failed_match.done["entries"] == 0
    finally:
        await controller.close()
    records = read_records(tail=1000, task_id=trimmed_match.request["task_id"])
    sent = next(record for record in records if record["event"] == "task.request_sent")
    assert sent["target"] == "local" and sent["request"]["scope"]["root"] == str(root)
    assert sent["request"]["query"]["value"] == TRACE
    assert sent["request"]["query"]["leading_whitespace"] == 0
    records = read_records(tail=1000, task_id=failed_match.request["task_id"])
    done = next(record for record in records if record["event"] == "task.done")
    assert done["entries"] == 0 and done["scope_complete"]
    assert any(record["event"] == "task.process_finished" for record in records)
    phases = [record for record in read_records(tail=1000, task_id=trimmed_match.request["task_id"]) if record["event"] == "task.phase"]
    assert {phase["stage"] for phase in phases} >= {"prefilter", "locate", "extract"}
    assert all(phase["elapsed"] >= 0 for phase in phases)
    assert "BUSINESS_BODY_MUST_NOT_BE_LOGGED" not in log_path().read_text()


async def test_logging_io_failure_does_not_break_search(tmp_path, monkeypatch):
    root = make_logs(tmp_path)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked))
    controller = Controller()
    assert controller.operations.error
    try:
        task = await controller.start(request(root=str(root), query=TRACE))
        _ = [event async for event in task.events()]
        assert task.done["scope_complete"] and len(task.entries) == 1
    finally:
        await controller.close()


async def test_ui_audit_records_submit_and_render_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    app = LogSearchApp(root=str(make_logs(tmp_path)))
    async with app.run_test(size=(80, 24)) as pilot:
        app.query_one("#query", Input).value = " " + TRACE + " "
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(.05)
            if app.search_task and app.search_task.done:
                break
        await pilot.pause(.3)
        assert app.query_one("#results", DataTable).row_count == 1
        assert app.query_one("#query", Input).value == TRACE
        submitted = next(record for record in read_records(tail=1000) if record["event"] == "ui.search_submitted")
        assert submitted["query"]["leading_whitespace"] == 1
        assert app.search_task.request["query"] == TRACE
        records = read_records(tail=1000, task_id=app.search_task.request["task_id"])
        rendered = [record for record in records if record["event"] == "ui.results_rendered"][-1]
        assert rendered["received_entries"] == 1 and rendered["visible_entries"] == 1
        await app.action_quit()
    assert read_records(tail=1)[0]["event"] == "session.end"
    assert "BUSINESS_BODY_MUST_NOT_BE_LOGGED" not in log_path().read_text()


def test_cli_jsonl_is_clean_and_diagnostics_filter_task(tmp_path):
    command = [sys.executable, "-m", "log_search", "search", "--local", "--root", str(make_logs(tmp_path)), "--query", TRACE, "--format", "jsonl"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert all("session_id" not in event for event in events)
    task_id = events[-1]["task_id"]
    result = subprocess.run([sys.executable, "-m", "log_search", "diagnostics", "--task-id", task_id, "--tail", "1000", "--format", "jsonl"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records and all(record["task_id"] == task_id for record in records)
    assert any(record["event"] == "cli.finished" and record["exit_code"] == 0 for record in records)
    assert "BUSINESS_BODY_MUST_NOT_BE_LOGGED" not in result.stdout


def test_cli_parse_error_is_recorded_without_raw_argument():
    result = subprocess.run([sys.executable, "-m", "log_search", "search", "--local", "--root", "/tmp", "--query", TRACE, "--timeout", "secret-not-a-number"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    records = read_records(tail=20)
    failure = next(record for record in records if record["event"] == "cli.arguments_invalid")
    assert failure["reason"] == "argument --timeout"
    assert "secret-not-a-number" not in log_path().read_text()


async def test_protocol_error_records_traceback_without_stdout_body(tmp_path, monkeypatch):
    root = make_logs(tmp_path)

    async def broken_worker():
        return await asyncio.create_subprocess_exec(sys.executable, "-c", "import sys; sys.stdin.buffer.readline(); print('NON_PROTOCOL_BUSINESS_CONTENT')", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)

    monkeypatch.setattr("log_search.controller.local_worker", broken_worker)
    controller = Controller()
    try:
        task = await controller.start(request(root=str(root), query=TRACE))
        _ = [event async for event in task.events()]
        assert task.done["outcome"] == "failed"
    finally:
        await controller.close()
    records = read_records(tail=1000)
    failure = next(record for record in records if record["event"] == "task.exception")
    assert failure["error_type"] == "JSONDecodeError" and "Traceback" in failure["traceback"]
    assert "NON_PROTOCOL_BUSINESS_CONTENT" not in log_path().read_text()

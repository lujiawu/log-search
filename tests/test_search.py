import asyncio
import gzip
import json
import subprocess
import sys
import threading

import pytest

from log_search import worker
from log_search.controller import Controller, request


@pytest.fixture
def logs(tmp_path):
    root = tmp_path / "deployment with space:and'quote"
    for name in ("app-beta", "app-alpha", "app-gamma"):
        (root / name / "logs" / "history").mkdir(parents=True)
    return root


def run(root, **kwargs):
    events = []
    worker.execute(request(root=str(root), query="TRACE", **kwargs), events.append)
    return [event["entry"] for event in events if event["type"] == "entry"], events[-1], events


def test_dense_hits_fill_the_limit_instead_of_returning_nothing(tmp_path):
    """单文件命中数远超 max_entries 时必须取满配额返回，而不是把整批结果丢掉。"""
    logs = tmp_path / "svc/logs/history"
    logs.mkdir(parents=True)
    line = "2026-01-01 10:00:%02d.000 [main] ERROR c.x.PropertyServiceImpl:202 - ### Error updating database. Cause: java.sql.SQLException TRACE\n"
    (logs / "error.2026-01-01.13.log").write_text("".join(line % (index % 60) for index in range(5000)))
    entries, done, _ = run(tmp_path)
    assert done["outcome"] == "limited" and done["entries"] == 100
    assert len(entries) == 100 and {entry["file"] for entry in entries} == {"svc/logs/history/error.2026-01-01.13.log"}


def test_nul_business_rolling_gzip_and_multiline(logs):
    (logs / "app-alpha/logs/history/debug.1").write_bytes(b"[thread] 2026-01-01 10:00:00.001 ERROR TRACE\n at example.one\n\x00TRACE\x00\n2026-01-01 10:00:01 INFO next\n")
    with gzip.open(logs / "app-gamma/logs/history/no-extension", "wb") as output:
        output.write(b'2026-01-01 10:00:00.002 INFO gateway\n{\n "id": "TRACE"\n}\n')
    (logs / "app-beta/logs/system.log").write_text("2026-01-01 10:00:00.003 INFO TRACE\nAuthorization: Bearer private-token\n")
    (logs / "database").mkdir()
    (logs / "database/data").write_text("TRACE")
    entries, done, _ = run(logs)
    assert done["scope_complete"]
    assert {entry["service"] for entry in entries} == {"app-alpha", "app-beta", "app-gamma"}
    detect = next(entry for entry in entries if entry["service"] == "app-alpha")
    assert detect["match_lines"] == [1, 3]
    assert "example.one" in "\n".join(detect["preview"])
    assert "private-token" not in json.dumps(entries)


def test_time_interval_and_unknown(logs):
    path = logs / "app-beta/logs/system.log"
    path.write_text("TRACE no timestamp\n2026-01-01 10:00:00 INFO TRACE\n2026-01-01 10:00:01 INFO TRACE\n2026-01-01 10:00:02 ERROR TRACE\n")
    entries, done, _ = run(logs, since="2026-01-01 10:00:00", until="2026-01-01 10:00:02")
    assert len(entries) == 2
    assert done["time_unknown"] == 1
    entries, _, _ = run(logs, since="2026-01-01 10:00:00", until="2026-01-01 10:00:02", include_unknown_time=True)
    assert len(entries) == 3
    with pytest.raises(ValueError):
        request(root=str(logs), query="TRACE", since="2026-02-30 10:00:00")


def test_huge_line_cross_chunk_and_late_match(logs):
    path = logs / "app-alpha/logs/history/rolling"
    prefix = b"2026-01-01 10:00:00 INFO "
    path.write_bytes(prefix + b"x" * (worker.CHUNK - len(prefix) - 2) + b"TRACE" + b"x" * (worker.CHUNK * 3) + b"\n2026-01-01 10:00:01 INFO next\n")
    entries, done, _ = run(logs)
    assert len(entries) == 1
    assert "TRACE" in "\n".join(entries[0]["preview"])
    assert entries[0]["preview_truncated"]
    assert done["scope_complete"] and not done["content_complete"]
    assert len(json.dumps(entries)) < 30000


def test_corrupt_archive_and_symlink_are_not_complete(logs, tmp_path):
    archive = logs / "app-gamma/logs/history/info.gz"
    archive.write_bytes(gzip.compress(b"2026-01-01 10:00:00 INFO TRACE\n")[:-5])
    (logs / "app-alpha/logs/link").symlink_to(tmp_path)
    _, done, _ = run(logs)
    assert not done["scope_complete"]
    assert done["unreadable_files"] == 1 and done["skipped_links"] == 1


def test_query_is_literal_and_no_archive(logs):
    (logs / "app-beta/logs/a:b").write_text('2026-01-01 10:00:00 INFO -x $(touch /tmp/not-created) "foo"\n')
    events = []
    worker.execute(request(root=str(logs), query='-x $(touch /tmp/not-created) "foo"'), events.append)
    assert sum(e["type"] == "entry" for e in events) == 1
    (logs / "app-gamma/logs/log.gz").write_bytes(gzip.compress(b"2026-01-01 10:00:00 INFO TRACE\n"))
    entries, done, _ = run(logs, include_archives=False)
    assert entries == [] and done["scope_complete"]


def test_query_trim_preserves_internal_spaces_and_rejects_empty(logs):
    (logs / "app-beta/logs/log").write_text("2026-01-01 10:00:00 INFO one  two\n2026-01-01 10:00:01 INFO one two\n")
    events = []
    value = request(root=str(logs), query=" \t one  two \n\u3000")
    assert value["query"] == "one  two"
    worker.execute(value, events.append)
    assert events[-1]["entries"] == 1
    for query in ("", " \t\n\u3000", "one\ntwo", "one\x00two"):
        with pytest.raises(ValueError):
            request(root=str(logs), query=query)


def test_limit_cancel_timeout(logs):
    (logs / "app-beta/logs/log").write_text("".join("2026-01-01 10:00:00 INFO TRACE\n" for _ in range(5)))
    entries, done, _ = run(logs, max_entries=2)
    assert len(entries) == 2 and done["outcome"] == "limited" and not done["scope_complete"]
    events = []
    stop = threading.Event()
    stop.set()
    worker.execute(request(root=str(logs), query="TRACE"), events.append, stop)
    assert events[-1]["outcome"] == "cancelled"
    _, done, _ = run(logs, timeout=.000001)
    assert done["outcome"] == "timed_out"


def test_sensitive_truncated_and_multiline_values(logs):
    (logs / "app-beta/logs/log").write_text('2026-01-01 10:00:00 INFO TRACE\n"Authorization":\n"Bearer private-value"\n2026-01-01 10:00:01 INFO ' + 'x' * 10000 + ' Authorization: TRACE-private-tail\n')
    entries, done, _ = run(logs)
    content = json.dumps(entries)
    assert "private-value" not in content and "TRACE-private-tail" not in content
    assert len(entries) == 2


def test_invalid_utf8_and_query_across_chunks(logs):
    prefix = b"2026-01-01 10:00:00 INFO "
    path = logs / "app-beta/logs/log"
    path.write_bytes(prefix + b"x" * (worker.CHUNK - len(prefix) - 1) + "中文TRACE".encode() + b"\xff\n")
    entries, done, _ = run(logs)
    assert len(entries) == 1 and done["decoding_errors"] == 1


async def test_controller_local_and_context(logs):
    path = logs / "app-beta/logs/log"
    path.write_text("2026-01-01 10:00:00 INFO before\n2026-01-01 10:00:01 ERROR TRACE\n at example\n2026-01-01 10:00:02 INFO after\n")
    controller = Controller()
    try:
        task = await controller.start(request(root=str(logs), query="TRACE"))
        events = [event async for event in task.events()]
        assert task.done["scope_complete"], events
        assert len(task.entries) == 1
        entry = task.entries[0]
        value = request("context", root=str(logs), file=entry["file"], line=entry["start_line"], source_identity=entry["source_identity"])
        result, done, _ = await controller.context(value)
        assert done["scope_complete"]
        assert len(result["entries"]) == 3
        path.write_text("changed")
        result, done, warnings = await controller.context(value)
        assert result is None and not done["scope_complete"]
        assert warnings[0]["code"] == "source_changed"
        invalid = request("context", root=str(logs), file="/etc/passwd", line=1)
        result, done, _ = await controller.context(invalid)
        assert result is None and done["outcome"] == "failed"
    finally:
        await controller.close()


async def test_controller_cancel_while_backpressured(logs):
    (logs / "app-beta/logs/log").write_text("2026-01-01 10:00:00 INFO TRACE\n" * 5000)
    controller = Controller()
    task = await controller.start(request(root=str(logs), query="TRACE", max_entries=10000))
    try:
        async for event in task.events():
            if event["type"] == "entry":
                await task.cancel()
                break
        await asyncio.wait_for(controller.close(), 8)
        assert task.done is not None and not task.done["scope_complete"]
        assert task.process.returncode is not None
    finally:
        await controller.close()


async def test_cancel_before_runner_starts(logs):
    controller = Controller()
    task = await controller.start(request(root=str(logs), query="TRACE"))
    await task.cancel()
    try:
        async def receive():
            return [event async for event in task.events()]
        events = await asyncio.wait_for(receive(), 2)
        assert events[-1]["outcome"] == "cancelled"
    finally:
        await controller.close()


def test_cli_jsonl_and_zero(logs):
    (logs / "app-beta/logs/log").write_text("2026-01-01 10:00:00 INFO TRACE\n")
    command = [sys.executable, "-m", "log_search", "search", "--local", "--root", str(logs), "--query", " \tTRACE \n", "--format", "jsonl", "--result-mode", "entries"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["type"] == "done" and events[-1]["entries"] == 1
    command[command.index("--query") + 1] = "no-match"
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout.splitlines()[-1])["entries"] == 0

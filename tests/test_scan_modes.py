import gzip
import hashlib
import threading

import pytest

from log_search import worker


def run(path, cancelled=None, **options):
    events = []
    request = dict(scope={"log_dirs": [str(path)]}, query="needle", **options)
    worker.execute(request, events.append, cancelled)
    return [e["entry"] for e in events if e["type"] == "entry"], events


@pytest.mark.parametrize("compressed", [False, True])
def test_default_physical_lines_and_full_hash(tmp_path, compressed):
    hit = b"  needle details\r\n"
    raw = b"2026-01-01 12:00:00 INFO start\n" + hit + b"tail\n"
    (tmp_path / "log").write_bytes(gzip.compress(raw) if compressed else raw)
    entries, events = run(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["content_kind"] == "line"
    assert entry["start_line"] == entry["end_line"] == 2
    assert entry["preview"] == ["  needle details"]
    assert entry["ts"] == "2026-01-01 12:00:00"
    assert entry["content_hash"] == hashlib.sha256(hit).hexdigest()
    assert entry["raw_bytes"] == len(hit) and entry["capture_complete"]
    assert events[-1]["result_mode"] == "lines"
    assert not any(e.get("stage") == "file_candidates" for e in events)


@pytest.mark.parametrize("options, hit", [
    ({"result_mode": "entries"}, b"needle"),
    ({"expand_pattern": "needle"}, b"needle"),
    ({}, b"java.lang.RuntimeException: needle"),
])
def test_entry_expansion(tmp_path, options, hit):
    raw = b"2026-01-01 12:00:00 ERROR begin\n" + hit + b"\n  at example.method(Main.java:1)\n"
    (tmp_path / "log").write_bytes(raw)
    entries, _ = run(tmp_path, **options)
    assert len(entries) == 1
    assert entries[0]["content_kind"] == "entry"
    assert (entries[0]["start_line"], entries[0]["end_line"]) == (1, 3)
    assert entries[0]["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert entries[0]["raw_bytes"] == len(raw)


def test_exception_expansion_can_be_disabled(tmp_path):
    (tmp_path / "log").write_text("2026-01-01 12:00:00 ERROR begin\nRuntimeException needle\n")
    entries, _ = run(tmp_path, auto_expand_exceptions=False)
    assert entries[0]["content_kind"] == "line"


def test_date_intersection_midnight_invalid_and_unknown(tmp_path):
    raw = "2026-01-02 01:00:00 INFO needle\n"
    for name in ("2026-01-01.log", "2026-01-02.log", "2026-01-03.log", "2026-02-30.log", "current.log"):
        (tmp_path / name).write_text(raw)
    entries, events = run(tmp_path, since="2026-01-01 23:00:00", until="2026-01-03 00:00:00")
    assert len(entries) == 4
    assert events[-1]["skipped_time_files"] == 1
    assert events[-1]["scope_complete"]
    assert events[-1]["scope_basis"] == "selected_files_after_filename_date_filter"
    excluded = [e for e in events if e.get("decision") == "exclude"]
    assert len(excluded) == 1 and excluded[0]["file"].endswith("2026-01-03.log")


def test_head_time_does_not_exclude_and_filter_can_be_disabled(tmp_path):
    raw = "2020-01-01 00:00:00 INFO old\n2026-01-02 01:00:00 INFO needle\n"
    (tmp_path / "current").write_text(raw)
    (tmp_path / "2020-01-01.log").write_text(raw)
    options = dict(since="2026-01-02 00:00:00", until="2026-01-03 00:00:00")
    entries, _ = run(tmp_path, **options)
    assert len(entries) == 1
    entries, events = run(tmp_path, filter_file_dates=False, **options)
    assert len(entries) == 2 and events[-1]["skipped_time_files"] == 0


@pytest.mark.parametrize("stop", [False, True])
def test_two_workers_and_cooperative_cancel(tmp_path, monkeypatch, stop):
    for index in range(6):
        (tmp_path / str(index)).write_text("needle\n")
    barrier = threading.Barrier(2, timeout=3)
    lock = threading.Lock()
    active, peak = [0], [0]
    original = worker.hit_rows
    cancelled = threading.Event()

    def rows(path, kind, runtime, before):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            barrier.wait()
            if stop:
                cancelled.set()
                runtime.check()
            yield from original(path, kind, runtime, before)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(worker, "hit_rows", rows)
    entries, events = run(tmp_path, cancelled, concurrency=99)
    assert peak[0] == 2 and active[0] == 0
    assert events[-1]["concurrency"] == 2
    assert events[-1]["outcome"] == ("cancelled" if stop else "completed")
    if not stop:
        assert len(entries) == events[-1]["entries"] == 6
        # Single native pass: no Python extraction reads, so bytes_read stays at zero.
        assert events[-1]["bytes_read"] == 0
        assert len([e for e in events if e.get("stage") == "locate" and e.get("file")]) == 6


@pytest.mark.parametrize("options", [{"expand_pattern": "["}, {"expand_pattern": "x" * 4097}, {"result_mode": "bad"}, {"concurrency": 0}])
def test_invalid_options_fail_before_scan(tmp_path, options):
    _, events = run(tmp_path, **options)
    assert events[-1]["outcome"] == "failed"


def test_catastrophic_regex_is_bounded(tmp_path):
    (tmp_path / "log").write_text("2026-01-01 12:00:00 INFO needle " + "a" * 300 + "!\n")
    entries, events = run(tmp_path, expand_pattern="(a+)+$", auto_expand_exceptions=False)
    assert not entries
    assert events[-1]["outcome"] == "limited"
    assert any(e.get("code") == "expand_pattern_timeout" for e in events)


def test_cancel_stops_all_native_processes(tmp_path, monkeypatch):
    import subprocess
    import sys

    for index in range(2):
        (tmp_path / str(index)).write_text("needle\n")
    cancelled = threading.Event()
    barrier = threading.Barrier(2, timeout=3)
    processes = []

    def rows(path, kind, runtime, before):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        runtime.process = process
        processes.append(process)
        try:
            barrier.wait()
            cancelled.set()
            runtime.check()
        finally:
            worker.stop_process(process)
            runtime.process = None
        yield from ()

    monkeypatch.setattr(worker, "hit_rows", rows)
    _, events = run(tmp_path, cancelled)
    assert events[-1]["outcome"] == "cancelled"
    assert len(processes) == 2 and all(p.poll() is not None for p in processes)


def test_corrupt_archive_counts_one_file_and_keeps_warnings(tmp_path):
    raw = gzip.compress(b"2026-01-01 12:00:00 INFO needle\n")
    (tmp_path / "log.gz").write_bytes(raw[:-5])
    entries, events = run(tmp_path)
    assert events[-1]["unreadable_files"] == 1
    assert len(entries) == 1  # lines decoded before the CRC failure remain
    warnings = [e for e in events if e["type"] == "warning"]
    assert {e["code"] for e in warnings} >= {"gzip_error"}  # no Python fallback pass exists anymore


@pytest.mark.parametrize("action, expected", [("check", "directory_check"), ("context", "context_file")])
def test_scope_basis_for_non_search_actions(tmp_path, action, expected):
    path = tmp_path / "log"
    path.write_text("2026-01-01 12:00:00 INFO needle\n")
    _, events = run(tmp_path, action=action, file=str(path), line=1)
    assert events[-1]["scope_basis"] == expected
    assert events[-1]["filter_file_dates"] is False


def test_parallel_entry_limit_never_overruns(tmp_path):
    for index in range(4):
        (tmp_path / str(index)).write_text("needle\n" * 20)
    entries, events = run(tmp_path, limits={"max_entries": 3})
    assert len(entries) == events[-1]["entries"] == 3
    assert events[-1]["outcome"] == "limited"


def test_later_exception_replaces_all_lines_for_same_entry(tmp_path):
    (tmp_path / "log").write_text("2026-01-01 12:00:00 WARNING needle begin\nplain needle\nRuntimeException needle\n  at example.method(Main.java:1)\n2026-01-01 12:00:01 WARNING needle next\n")
    entries, _ = run(tmp_path)
    assert len(entries) == 2
    assert len({e["id"] for e in entries}) == 2
    expanded = next(e for e in entries if e["content_kind"] == "entry")
    assert (expanded["start_line"], expanded["end_line"]) == (1, 4)
    assert all(e["level"] == "WARN" for e in entries)

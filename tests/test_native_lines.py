import datetime
import gzip
import os
import threading

from log_search import worker


def run(path, cancelled=None, **options):
    events = []
    request = dict(scope={"log_dirs": [str(path)]}, query="needle", **options)
    worker.execute(request, events.append, cancelled)
    return [e["entry"] for e in events if e["type"] == "entry"], events


def test_deep_continuation_hit_inherits_head_time(tmp_path):
    lines = ["2026-11-25 13:43:46.752  [reactor] INFO cn.xm.star.gateway.AccessLogFilter:97 - [writeAccessLog][网关日志：{"]
    lines += ['  "field%d" : "%d",' % (index, index) for index in range(30)]
    lines.append('  "responseHeaders" : "{\\"X-Request-Id\\":\\"needle\\"}"')
    lines += ['  "httpStatus" : "OK"', "}"]
    (tmp_path / "gw.log").write_text("\n".join(lines) + "\n")
    entries, _ = run(tmp_path, since="2026-11-25 00:00:00", until="2026-11-26 00:00:00")
    assert len(entries) == 1
    assert entries[0]["ts"] == "2026-11-25 13:43:46.752000"
    assert entries[0]["content_kind"] == "line"


def test_hit_beyond_context_window_is_time_unknown(tmp_path):
    lines = ["2026-11-25 13:43:46.752 INFO open"] + ["padding %d" % index for index in range(40)] + ["needle deeper"]
    (tmp_path / "deep.log").write_text("\n".join(lines) + "\n")
    entries, events = run(tmp_path)
    assert len(entries) == 1 and entries[0]["ts"] is None
    assert events[-1]["time_unknown"] == 1
    entries, _ = run(tmp_path, since="2026-11-25 00:00:00", until="2026-11-26 00:00:00")
    assert entries == []
    entries, _ = run(tmp_path, since="2026-11-25 00:00:00", until="2026-11-26 00:00:00", include_unknown_time=True)
    assert len(entries) == 1


def test_line_hit_in_same_entry_as_exception_is_replaced(tmp_path):
    text = ("2026-01-01 12:00:00 INFO start needle\n"
            "java.lang.RuntimeException: needle failed\n"
            "\tat com.acme.Service.run(Service.java:10)\n")
    (tmp_path / "log").write_text(text)
    entries, events = run(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["content_kind"] == "entry" and entry["start_line"] == 1
    assert any("Service.java" in line for line in entry["preview"])


def test_secret_block_before_hit_masks_line(tmp_path):
    text = ("2026-01-01 12:00:00 INFO request\n"
            '  "sso_token": "\n'
            "  needle leaked-value\n")
    (tmp_path / "log").write_text(text)
    entries, _ = run(tmp_path)
    assert len(entries) == 1
    assert entries[0]["preview"] == ["[REDACTED: 多行敏感字段之后的内容]"]


def test_oversized_line_marks_truncated(tmp_path):
    line = "2026-01-01 12:00:00 INFO " + "a" * 40000 + " needle tail"
    (tmp_path / "big.log").write_text(line + "\n")
    entries, events = run(tmp_path)
    assert len(entries) == 1
    assert entries[0]["preview_truncated"] and entries[0]["ts"] == "2026-01-01 12:00:00"
    # Truncated lines must not fold into aggregates.
    assert events[-1]["truncated_entries"] == 1


def test_hit_budget_stops_native_scan_limited(tmp_path):
    (tmp_path / "log").write_text("\n".join("2026-01-01 12:00:%02d INFO needle %d" % (index % 60, index) for index in range(60)) + "\n")
    entries, events = run(tmp_path, limits=dict(max_entries=3, max_bytes=32 * 1024 * 1024, timeout_seconds=60))
    assert len(entries) == 3 and events[-1]["outcome"] == "limited"
    assert events[-1]["bytes_read"] == 0  # the native pass never counts Python bytes


def test_gzip_single_pass_hits(tmp_path):
    (tmp_path / "log.gz").write_bytes(gzip.compress(b"2026-01-01 12:00:00 INFO needle gz\n2026-01-01 12:00:01 INFO other\n"))
    entries, events = run(tmp_path)
    assert len(entries) == 1 and entries[0]["content_kind"] == "line"
    assert events[-1]["outcome"] == "completed"


def test_cancel_stops_native_hit_rows_processes(tmp_path, monkeypatch):
    import subprocess
    import sys

    for index in range(2):
        (tmp_path / ("log%d" % index)).write_text("2026-01-01 12:00:0%d INFO needle\n" % index)
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
    assert len(processes) == 2 and all(process.poll() is not None for process in processes)


def stamp(path, head_hour):
    path.write_text("2026-01-04 %02d:00:00 INFO needle %s\n" % (head_hour, path.name))
    ts = datetime.datetime(2026, 1, 4, head_hour, 30).timestamp()
    os.utime(path, (ts, ts))


def stamp_day(path, day):
    path.write_text("2026-01-%02d 08:00:00 INFO needle %s\n" % (day, path.name))
    ts = datetime.datetime(2026, 1, day, 8, 30).timestamp()
    os.utime(path, (ts, ts))


def names(entries):
    return sorted(os.path.basename(entry["file"]) for entry in entries)


def test_family_mtime_prunes_old_rotations(tmp_path):
    for day in range(1, 7):
        stamp_day(tmp_path / ("app.log.%d" % day), day)
    entries, events = run(tmp_path, since="2026-01-04 00:00:00", until="2026-01-05 00:00:00")
    assert names(entries) == ["app.log.4"]
    assert events[-1]["skipped_time_files"] == 5
    located = [e for e in events if e.get("stage") == "locate" and e.get("file")]
    assert len(located) == 1


def test_contradictory_mtime_keeps_family(tmp_path):
    for day in (1, 2, 3, 4):
        stamp_day(tmp_path / ("app.log.%d" % day), day)
    swapped = tmp_path / "app.log.3"
    ts = datetime.datetime(2026, 1, 2, 7, 30).timestamp()
    os.utime(swapped, (ts, ts))  # mtime now decreases with rotation index
    _, events = run(tmp_path, since="2026-01-03 00:00:00", until="2026-01-05 00:00:00")
    assert events[-1]["skipped_time_files"] == 0
    located = [e for e in events if e.get("stage") == "locate" and e.get("file")]
    assert len(located) == 4


def test_within_day_gz_rotations_prune_to_window(tmp_path):
    for index in range(8):
        path = tmp_path / ("gw.2026-01-04.%d.gz" % index)
        body = ("2026-01-04 %02d:00:00 INFO needle %d\n" % (index + 1, index)).encode()
        path.write_bytes(gzip.compress(body))
        ts = datetime.datetime(2026, 1, 4, index + 1, 30).timestamp()
        os.utime(path, (ts, ts))
    entries, events = run(tmp_path, since="2026-01-04 03:00:00", until="2026-01-04 05:00:00")
    assert names(entries) == ["gw.2026-01-04.2.gz", "gw.2026-01-04.3.gz"]
    assert events[-1]["skipped_time_files"] == 6
    located = [e for e in events if e.get("stage") == "locate" and e.get("file")]
    assert len(located) == 2


def test_entries_mode_stops_after_last_hit(tmp_path):
    head = "2026-01-01 12:00:00 INFO "
    body = head + "needle early\n" + "".join(head + "filler %d\n" % index for index in range(5000))
    (tmp_path / "log").write_text(body)
    total = os.path.getsize(tmp_path / "log")
    entries, events = run(tmp_path, result_mode="entries")
    assert len(entries) == 1 and entries[0]["content_kind"] == "entry"
    # The tail after the last hit's closing header must not be decompressed/read.
    assert events[-1]["bytes_read"] < total // 2

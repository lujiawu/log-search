import gzip
import hashlib
import json
import subprocess
import sys
import time

import pytest

from log_search import worker
from log_search.controller import request


def search(tmp_path, **kwargs):
    events = []
    worker.execute(request(log_dirs=[str(tmp_path)], query="TRACE", **kwargs), events.append)
    return [event["entry"] for event in events if event["type"] == "entry"], events


@pytest.mark.parametrize("compressed", [False, True])
def test_only_matching_entries_are_materialized(tmp_path, monkeypatch, compressed):
    content = (b"2026-01-01 10:00:00 INFO irrelevant\n" * 2000
               + b"2026-01-01 10:00:01 WARN target\r\n  TRACE\x00TRACE\r\n"
               + b"2026-01-01 10:00:02 INFO irrelevant\n" * 2000)
    (tmp_path / "log").write_bytes(gzip.compress(content) if compressed else content)
    original = worker.Entry
    constructed = []

    def tracked(*args, **kwargs):
        constructed.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(worker, "Entry", tracked)
    entries, events = search(tmp_path)
    assert constructed == [2001]
    assert len(entries) == 1
    entry = entries[0]
    raw = b"2026-01-01 10:00:01 WARN target\r\n  TRACE\x00TRACE\r\n"
    assert entry["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert entry["raw_bytes"] == len(raw)
    assert entry["capture_complete"] is True
    assert entry["match_lines"] == [2002]
    assert events[-1]["scope_complete"]


def test_truncated_previews_have_distinct_full_hashes(tmp_path):
    head = b"2026-01-01 10:00:00 INFO TRACE\n"
    for index in range(2):
        (tmp_path / str(index)).write_bytes(head + b"x\n" * 200 + str(index).encode())
    entries, _ = search(tmp_path)
    assert entries[0]["preview"] == entries[1]["preview"]
    assert entries[0]["content_hash"] != entries[1]["content_hash"]
    assert all(entry["capture_complete"] and entry["preview_truncated"] for entry in entries)


@pytest.mark.parametrize("compressed", [False, True])
def test_late_hit_preserves_preview_and_sensitive_state(tmp_path, compressed):
    raw = (b"2026-01-01 10:00:00 INFO begin\n" + b"ordinary\n" * 180
           + b'"Authorization":\n"TRACE-private-value"\n')
    (tmp_path / "log").write_bytes(gzip.compress(raw) if compressed else raw)
    entries, events = search(tmp_path)
    entry = entries[0]
    assert entry["start_line"] == 1 and entry["end_line"] == 183
    assert entry["match_lines"] == [183]
    assert entry["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert entry["raw_bytes"] == len(raw)
    assert "private-value" not in json.dumps(entries)
    assert events[-1]["scope_complete"]


def test_date_and_header_are_priority_hints_only(tmp_path):
    raw = b"2020-01-01 00:00:00 INFO old\n2026-01-01 10:00:00 INFO TRACE\n"
    (tmp_path / "2020-01-01.log").write_bytes(raw)
    (tmp_path / "2020-01-01.log.gz").write_bytes(gzip.compress(raw))
    (tmp_path / "unknown").write_bytes(b"TRACE unknown\n")
    entries, events = search(tmp_path, since="2026-01-01 10:00:00", until="2026-01-01 10:00:01", include_unknown_time=True)
    assert len(entries) == 3
    phases = [event for event in events if event["type"] == "phase"]
    assert {event["stage"] for event in phases} == {"prefilter", "locate", "extract"}
    assert all("TRACE" not in json.dumps(event) for event in phases)
    assert all(event["decision"] == "retain" for event in phases if event["stage"] == "prefilter")


def test_mutation_after_scan_is_flagged_incomplete(tmp_path, monkeypatch):
    path = tmp_path / "log"
    path.write_bytes(b"2026-01-01 10:00:00 INFO TRACE\n")
    original = worker.hit_rows

    def mutate(path_, kind_, runtime_, before_):
        # Single pass: the result is the read snapshot; the later change must still surface.
        entries = list(original(path_, kind_, runtime_, before_))
        path.write_bytes(b"2026-01-01 10:00:00 INFO other\n2026-01-01 10:00:01 INFO TRACE\n")
        yield from entries

    monkeypatch.setattr(worker, "hit_rows", mutate)
    entries, events = search(tmp_path)
    assert len(entries) == 1 and entries[0]["start_line"] == 1
    assert not events[-1]["scope_complete"]
    assert events[-1]["source_changed"] >= 1


def test_mutation_after_lines_scan_marks_scope_incomplete(tmp_path, monkeypatch):
    path = tmp_path / "log"
    path.write_bytes(b"2026-01-01 10:00:00 INFO TRACE\n")
    original = worker.hit_rows

    def mutate(path_, kind_, runtime_, before_):
        entries = list(original(path_, kind_, runtime_, before_))
        path.write_bytes(b"2026-01-01 10:00:00 INFO TRACE\n2026-01-01 10:00:01 INFO TRACE\n")
        yield from entries

    monkeypatch.setattr(worker, "hit_rows", mutate)
    entries, events = search(tmp_path, result_mode="lines")
    assert len(entries) == 1
    assert entries[0]["capture_complete"] and not entries[0]["source_changed"]
    assert events[-1]["source_changed"] >= 1 and not events[-1]["scope_complete"]


def test_first_limited_entry_after_mutation_keeps_snapshot_honest(tmp_path, monkeypatch):
    path = tmp_path / "log"
    path.write_bytes(b"2026-01-01 10:00:00 INFO TRACE\n")
    original = worker.hit_rows

    def mutate(path_, kind_, runtime_, before_):
        entries = list(original(path_, kind_, runtime_, before_))
        path.write_bytes(b"2026-01-01 10:00:00 INFO TRACE\n2026-01-01 10:00:01 INFO TRACE\n")
        yield from entries

    monkeypatch.setattr(worker, "hit_rows", mutate)
    entries, events = search(tmp_path, max_entries=1)
    assert len(entries) == 1 and events[-1]["outcome"] == "limited"
    assert entries[0]["capture_complete"]  # the emitted snapshot itself was read intact


def test_oversized_hit_line_falls_back_with_exact_hash(tmp_path):
    raw = b"2026-01-01 10:00:00 INFO " + b"x" * (worker.CHUNK * 5) + "中文TRACE".encode() + b"\xff"
    path = tmp_path / "log"
    path.write_bytes(raw)
    runtime = worker.Runtime(request(log_dirs=[str(tmp_path)], query="中文TRACE", result_mode="entries"), lambda event: None)
    entries = list(worker.hit_rows(path, "plain", runtime, worker.identity(path)))
    assert entries[0].content_hash == hashlib.sha256(raw).hexdigest()
    assert entries[0].raw_bytes == len(raw)
    assert "中文TRACE" in "\n".join(entries[0].view())
    assert runtime.counts["decoding_errors"] == 1


@pytest.mark.parametrize("secret_line", [2, 149, 151, 180])
def test_indexed_preview_matches_ordinary_parser(tmp_path, secret_line):
    lines = [b"2026-01-01 10:00:00 ERROR begin"] + [b"ordinary"] * 200
    lines[secret_line - 1] = b'"Authorization":'
    lines[190] = b"TRACE-hidden"
    lines.append(b"2026-01-01 10:00:01 INFO TRACE")
    raw = b"\r\n".join(lines)
    path = tmp_path / "log"
    path.write_bytes(raw)
    runtime = worker.Runtime(request(log_dirs=[str(tmp_path)], query="TRACE", result_mode="entries"), lambda event: None)
    before = worker.identity(path)
    windowed = list(worker.hit_rows(path, "plain", runtime, before))
    ordinary = [entry for entry in worker.parsed_entries(path, "plain", b"TRACE", runtime, before=before) if entry.matched]
    assert [worker.record(entry, path, tmp_path, before, runtime) for entry in windowed] == [
        worker.record(entry, path, tmp_path, before, runtime) for entry in ordinary]


def test_file_candidates_phase_excludes_extraction(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"TRACE\n")
    events = []
    runtime = worker.Runtime(request(log_dirs=[str(tmp_path)], query="TRACE"), events.append)
    candidates = worker.candidates([(path, tmp_path)], runtime)
    assert next(candidates) == (path, tmp_path)
    phase = next(event for event in events if event["type"] == "phase")
    assert phase["stage"] == "file_candidates"
    assert phase["file"] is None and phase["files_count"] == 1
    assert phase["elapsed"] >= 0
    assert "bytes_read" not in phase and "TRACE" not in json.dumps(phase)
    assert runtime.process is None
    assert list(candidates) == []


@pytest.mark.parametrize("stop", ["timeout", "cancel", "limit"])
def test_extract_phase_before_terminal_event(tmp_path, monkeypatch, stop):
    (tmp_path / "log").write_bytes(b"2026-01-01 10:00:00 INFO TRACE\n")
    original = worker.parsed_entries

    def interrupted(*args, **kwargs):
        runtime = args[3]
        if stop == "timeout":
            runtime.deadline = 0
            runtime.check()
        elif stop == "cancel":
            runtime.cancelled.set()
            runtime.check()
        yield from original(*args, **kwargs)

    monkeypatch.setattr(worker, "parsed_entries", interrupted)
    _, events = search(tmp_path, max_entries=1)
    assert events[-1]["outcome"] == {"timeout": "timed_out", "cancel": "cancelled", "limit": "limited"}[stop]
    phase = next(event for event in events if event.get("stage") == "extract")
    assert phase["elapsed"] >= 0 and phase["bytes_read"] >= 0
    assert events.index(phase) < len(events) - 1


def test_pending_preview_admission_matches_original_after_large_lines(tmp_path):
    path = tmp_path / "log"
    raw = (b"2026-01-01 10:00:00 INFO begin\n" + (b"x" * 20000 + b"\n") * 2
           + b"ordinary\n" * 147 + b"late-small-line\nTRACE\n")
    path.write_bytes(raw)
    runtime = worker.Runtime(request(log_dirs=[str(tmp_path)], query="TRACE"), lambda event: None)
    before = worker.identity(path)
    hits = {152}  # TRACE on the last line; coordinates now arrive from the single native pass
    ordinary = list(worker.parsed_entries(path, "plain", b"TRACE", runtime, before=before))
    indexed = list(worker.parsed_entries(path, "plain", b"TRACE", runtime, hits=hits, before=before))
    assert "late-small-line" in ordinary[0].preview
    assert worker.record(indexed[0], path, tmp_path, before, runtime) == worker.record(
        ordinary[0], path, tmp_path, before, runtime)


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
def test_gzip_head_is_interruptible_without_output(tmp_path, monkeypatch, stop):
    original = subprocess.Popen
    children = []

    def blocked_gzip(args, **kwargs):
        assert args[:2] == ["gzip", "-cd"]
        process = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", blocked_gzip)
    runtime = worker.Runtime(request(log_dirs=[str(tmp_path)], query="TRACE"), lambda event: None)
    if stop == "timeout":
        runtime.deadline = time.monotonic() + .01
    else:
        runtime.cancelled.set()
    started = time.monotonic()
    with pytest.raises(worker.Stopped):
        worker.gzip_head(tmp_path / "log.gz", runtime)
    assert time.monotonic() - started < 2
    assert runtime.process is None and children[0].poll() is not None


def test_gzip_head_handles_empty_members_and_bounds_output(tmp_path):
    path = tmp_path / "log.gz"
    payload = b"2026-01-01 10:00:00 INFO TRACE\n" + b"x" * (worker.CHUNK * 2)
    path.write_bytes(gzip.compress(b"") * 100 + gzip.compress(payload) + b"\0" * 10000)
    runtime = worker.Runtime(request(log_dirs=[str(tmp_path)], query="TRACE"), lambda event: None)
    assert worker.gzip_head(path, runtime) == payload[:worker.CHUNK]
    assert runtime.process is None

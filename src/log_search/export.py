"""Bounded, redacted exports bound to the submitted task snapshot."""

import json

from .worker import redact


def export_task(task, entries=None, format="jsonl"):
    entries = task.entries if entries is None else entries
    metadata = dict(type="task", target=task.host or "local", request=task.request, started_at=task.started_at, finished_at=task.finished_at, result=task.done, scope_complete=bool(task.done and task.done.get("scope_complete")), warnings=task.warnings, redaction_version=1, exported_entries=len(entries))
    if format == "jsonl":
        # Metadata carries the original query. Credentials are never part of it.
        lines = [json.dumps(metadata, ensure_ascii=True)]
        for entry in entries:
            safe = dict(entry, preview=[redact(line) for line in entry["preview"]])
            lines.append(json.dumps(dict(type="entry", entry=safe), ensure_ascii=True))
        return "\n".join(lines) + "\n"
    lines = [json.dumps(metadata, ensure_ascii=True)]
    for entry in entries:
        lines.extend(["", "{}:{} [{}] {}".format(entry["file"], entry["start_line"], entry["service"], entry.get("ts") or "时间未知"), *[redact(line) for line in entry["preview"]]])
    return "\n".join(lines) + "\n"

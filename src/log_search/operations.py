"""Structured local diagnostics, shared safely by concurrent Linux processes."""

import collections
import datetime
import fcntl
import hashlib
import json
import os
import platform
import re
import sys
import traceback
import uuid
from pathlib import Path

from . import __version__
from .worker import redact

MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 4
TRACE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
SENSITIVE = re.compile(r"(?:authorization|proxy.authorization|cookies?|set.cookie|[\w-]*(?:password|passphrase|secret|token|api.key|private.key))", re.I)


def log_path():
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "log-search" / "operations.jsonl"


def source_fingerprint():
    digest = hashlib.sha256()
    try:
        for path in sorted(Path(__file__).parent.glob("*.py")):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    except OSError:
        return None
    return digest.hexdigest()


def query_info(value):
    """UUID trace queries are diagnostic evidence; arbitrary literals may be secrets."""
    raw = value.encode("utf-8", "replace")
    trace = bool(TRACE_ID.fullmatch(value.strip()))
    return dict(kind="trace_id" if trace else "literal", value=value if trace else "[OMITTED]", sha256=hashlib.sha256(raw).hexdigest(), characters=len(value), bytes=len(raw), leading_whitespace=len(value) - len(value.lstrip()), trailing_whitespace=len(value) - len(value.rstrip()))


def safe(value, key="", depth=0):
    if depth > 8:
        return "[DEPTH LIMIT]"
    if SENSITIVE.fullmatch(key):
        return "[REDACTED]"
    if key in ("preview", "body", "content", "clipboard", "stdout", "source_code"):
        return "[OMITTED]"
    if isinstance(value, str):
        if key in ("query", "text", "filter_text"):
            return query_info(value)
        return redact(value[:8192])
    if isinstance(value, dict):
        return {str(k): safe(v, str(k), depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [safe(item, depth=depth + 1) for item in value[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value)[:8192])


class OperationLog:
    def __init__(self, role="controller", *, path=None, max_bytes=MAX_BYTES, backups=BACKUPS):
        self.path = Path(path) if path is not None else log_path()
        self.session_id = uuid.uuid4().hex
        self.role = role
        self.max_bytes, self.backups = max_bytes, backups
        self.error = None
        self.closed = False
        self.emit("session.start", version=__version__, python=platform.python_version(), executable=sys.executable, platform=platform.system(), cwd=str(Path.cwd()), package_path=str(Path(__file__).parent), source_sha256=source_fingerprint())

    def emit(self, event, *, level="info", **fields):
        if self.closed:
            return False
        lock = output = None
        try:
            record = dict(time=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"), level=level, event=event, session_id=self.session_id, pid=os.getpid(), role=self.role, **safe(fields))
            block = (json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.parent.is_symlink():
                raise OSError("操作日志目录不能是符号链接")
            self.path.parent.chmod(0o700)
            # Each write opens the current file under a cross-process rotation lock.
            lock = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists() and self.path.stat().st_size + len(block) > self.max_bytes:
                for index in range(self.backups, 0, -1):
                    source = self.path if index == 1 else Path(str(self.path) + "." + str(index - 1))
                    if source.exists():
                        source.replace(str(self.path) + "." + str(index))
            output = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            os.fchmod(output, 0o600)
            while block:
                block = block[os.write(output, block):]
            self.error = None
            return True
        except (OSError, ValueError, TypeError) as exc:
            # Diagnostics must not break log search or pollute JSON protocol stdout.
            self.error = redact(str(exc))
            return False
        finally:
            if output is not None:
                os.close(output)
            if lock is not None:
                os.close(lock)

    def exception(self, event, exc, **fields):
        self.emit(event, level="error", error_type=type(exc).__name__, message=str(exc), traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), **fields)

    def close(self):
        if not self.closed:
            self.emit("session.end")
            self.closed = True


def read_records(*, path=None, tail=100, session_id=None, task_id=None):
    path = Path(path) if path is not None else log_path()
    if not 1 <= tail <= 1000:
        raise ValueError("tail 必须为 1～1000")
    result = collections.deque(maxlen=tail)
    for filename in [Path(str(path) + "." + str(index)) for index in range(BACKUPS, 0, -1)] + [path]:
        try:
            with filename.open("r", encoding="ascii") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue  # A process may have died midway through one record.
                    if not isinstance(record, dict):
                        continue
                    if session_id and record.get("session_id") != session_id:
                        continue
                    if task_id and record.get("task_id") != task_id:
                        continue
                    result.append(record)
        except FileNotFoundError:
            continue
    return list(result)

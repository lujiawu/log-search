"""Self-contained standard-library worker, also sent verbatim through SSH stdin.

Keep this module compatible with Python 3.8. No application-package imports.
"""

import collections
import concurrent.futures
import bisect
import codecs
import datetime
import gzip
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

PROTOCOL = 1
VERSION = "0.1.0"
CHUNK = 65536
MAX_MESSAGE = 1024 * 1024
HEAD = re.compile(rb"^\s*(?:\[[^\]\r\n]{0,256}\])?\s*(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d{1,6})?)(?:\s*(Z|[+-]\d\d:\d\d))?")
LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b")
CONTROL = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|[\x01-\x08\x0b-\x1f\x7f]")
SECRET = re.compile(r"(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie|[\w-]{0,64}(?:token|password|secret|api[-_]?key))\s*[\"']?\s*[:=]\s*)([^\r\n]*)")
SECRET_WORDS = (b"authorization", b"cookie", b"token", b"password", b"secret", b"api-key", b"api_key", b"apikey")
EXCEPTION = re.compile(r"(?:\b[\w.$]*(?:Exception|Error)\b|^\s*(?:at\s+[\w.$]+\(|Caused by:|Suppressed:|Traceback \())")


def clean(text):
    return CONTROL.sub("", text.replace("\x00", "␀"))


def redact(text):
    # Mask the remainder of a sensitive physical line, including truncated JSON.
    return SECRET.sub(lambda match: match.group(1) + "[REDACTED]", clean(text))


def parse_time(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d{1,6})?", value):
        raise ValueError("时间格式应为 YYYY-MM-DD HH:mm:ss[.SSS]，使用日志原始时间")
    return datetime.datetime.fromisoformat(value.replace(",", "."))


def validate(request):
    """Validate a request and trim search boundaries consistently at every entry."""
    if not isinstance(request, dict) or request.get("protocol", 1) != PROTOCOL:
        raise ValueError("不支持的协议")
    action = request.get("action", "search")
    if action not in ("search", "context", "check", "logs", "exception-stats"):
        raise ValueError("不支持的操作")
    scope = request.get("scope", {})
    if not isinstance(scope, dict):
        raise ValueError("scope 必须是对象")
    root, directories = scope.get("root"), scope.get("log_dirs")
    if not root and not directories:
        raise ValueError("请选择部署根目录或指定日志目录")
    if directories is not None and (not isinstance(directories, list) or not directories):
        raise ValueError("log_dirs 必须是非空数组")
    paths = ([root] if root else []) + list(directories or [])
    if any(not isinstance(p, str) or not p.startswith("/") or "\x00" in p for p in paths):
        raise ValueError("日志路径必须为绝对路径")
    if root and directories and any(Path(root) not in Path(p).parents for p in directories):
        raise ValueError("指定日志目录必须位于部署根目录内")
    if action == "search":
        for key in ("filter_file_dates", "auto_expand_exceptions"):
            if not isinstance(request.get(key, True), bool):
                raise ValueError(key + " 必须是布尔值")
        if request.get("result_mode", "lines") not in ("lines", "entries"):
            raise ValueError("result_mode 必须是 lines 或 entries")
        concurrency = request.get("concurrency", 2)
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError("concurrency 必须是正整数")
        request["concurrency"] = min(2, concurrency)
        pattern = request.get("expand_pattern", "")
        if not isinstance(pattern, str) or len(pattern.encode("utf-8")) > 4096:
            raise ValueError("expand_pattern 不能超过 4 KiB")
        try:
            re.compile(pattern)
        except (re.error, RecursionError) as exc:
            raise ValueError("非法展开正则: " + str(exc))
        query = request.get("query", "")
        if isinstance(query, str):
            query = query.strip()
            request["query"] = query
        if not isinstance(query, str) or not query.strip() or any(c in query for c in "\r\n\x00"):
            raise ValueError("查询值必填，不能含换行或 NUL")
        if len(query.encode("utf-8")) > 4096:
            raise ValueError("查询值不能超过 4 KiB")
        since, until = parse_time(request.get("since")), parse_time(request.get("until"))
        if since is not None and until is not None and since >= until:
            raise ValueError("开始时间必须早于结束时间")
    if action == "exception-stats":
        since, until = parse_time(request.get("since")), parse_time(request.get("until"))
        if since is not None and until is not None and since >= until:
            raise ValueError("开始时间必须早于结束时间")
        top = request.get("top", 10)
        if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 1000:
            raise ValueError("top 必须是 1～1000 的整数")
    if action == "context":
        filename = request.get("file", "")
        if not isinstance(filename, str) or not filename or "\x00" in filename or ".." in Path(filename).parts:
            raise ValueError("非法上下文路径")
        if not isinstance(request.get("line"), int) or not 1 <= request["line"] <= 10**12:
            raise ValueError("行号必须是正整数")
    limits = request.get("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("limits 必须是对象")
    for key, default, maximum in (("max_entries", 1000, 100000), ("timeout_seconds", 60, 3600), ("max_bytes", 32 * 1024 * 1024, 128 * 1024 * 1024)):
        value = limits.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value <= maximum:
            raise ValueError("非法资源限额: " + key)
    return request


class Stopped(Exception):
    pass


class Runtime:
    def __init__(self, request, emit, cancelled=None):
        self.request = request
        self.emit_callback = emit
        self.cancelled = cancelled or threading.Event()
        self.deadline = time.monotonic() + request.get("limits", {}).get("timeout_seconds", 60)
        self.outcome = "completed"
        self.counts = dict(discovered=0, scanned=0, candidates=0, bytes_read=0, entries=0, unreadable_files=0, unsupported_files=0, skipped_links=0, source_changed=0, time_unknown=0, truncated_entries=0, decoding_errors=0)
        self.payload_bytes = 0
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.emit_lock = threading.RLock()
        self.local = threading.local()
        self.processes = set()
        self.unreadable_paths = set()
        self.stopped = threading.Event()
        self.counts["skipped_time_files"] = 0
        self.last_progress = 0
        self.process = None
        self.resource_checked = 0
        self.entry_limit = int(request.get("limits", {}).get("max_entries", 100))
        self.byte_limit = int(request.get("limits", {}).get("max_bytes", 32 * 1024 * 1024))

    @property
    def process(self):
        return getattr(self.local, "process", None)

    @process.setter
    def process(self, process):
        with self.lock:
            old = getattr(self.local, "process", None)
            self.processes.discard(old)
            self.local.process = process
            if process is not None:
                self.processes.add(process)

    def increment(self, key, amount=1):
        with self.lock:
            self.counts[key] += amount
        if key == "bytes_read":
            self.local.bytes_read = getattr(self.local, "bytes_read", 0) + amount

    def unreadable(self, path):
        with self.lock:
            key = str(path)
            if key not in self.unreadable_paths:
                self.unreadable_paths.add(key)
                self.counts["unreadable_files"] += 1

    def stop_all(self):
        with self.lock:
            processes = tuple(self.processes)
        for process in processes:
            stop_process(process)

    def check(self):
        if self.cancelled.is_set():
            self.outcome = "cancelled"
            raise Stopped()
        if time.monotonic() >= self.deadline:
            self.outcome = "timed_out"
            raise Stopped()
        if self.stopped.is_set():
            raise Stopped()
        processes = ()
        if time.monotonic() - self.resource_checked > .1:
            with self.lock:
                if time.monotonic() - self.resource_checked > .1:
                    self.resource_checked = time.monotonic()
                    processes = tuple(self.processes)
        for process in processes:
            try:
                pages = int(Path("/proc/{}/statm".format(process.pid)).read_text().split()[1])
                if pages * os.sysconf("SC_PAGE_SIZE") > 512 * 1024 * 1024:
                    self.warning("resource_limit", "rg 内存超过 512 MiB")
                    self.outcome = "limited"
                    self.stopped.set()
                    raise Stopped()
            except (OSError, ValueError, IndexError):
                pass

    def emit(self, kind, **data):
        with self.emit_lock:
            self.emit_callback(dict(type=kind, **data))

    def warning(self, code, message, path=None):
        self.emit("warning", code=code, message=message, file=str(path) if path is not None else None)

    def phase(self, stage, path, started, **data):
        self.emit("phase", stage=stage, file=str(path) if path is not None else None,
                  elapsed=round(time.monotonic() - started, 6), **data)

    def progress(self, force=False):
        self.check()
        now = time.monotonic()
        with self.lock:
            if not force and now - self.last_progress <= .25:
                return
            self.last_progress = now
            counts = dict(self.counts)
        self.emit("progress", **counts, elapsed=round(now - self.started, 2))

    def expansion_matches(self, text):
        """Keep user regex CPU consumption outside the worker's threads/GIL."""
        pattern = self.request.get("expand_pattern", "")
        if not pattern:
            return False
        code = "import json,re,sys; p,t=json.load(sys.stdin); sys.exit(0 if re.search(p,t) else 1)"
        process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        with self.lock:
            self.processes.add(process)
        started = time.monotonic()
        payload = json.dumps([pattern, text[:16384]]).encode("utf-8")
        try:
            while True:
                self.check()
                try:
                    process.communicate(input=payload, timeout=.05)
                    return process.returncode == 0
                except subprocess.TimeoutExpired:
                    payload = None
                    if time.monotonic() - started >= .5:
                        self.warning("expand_pattern_timeout", "展开正则超过单行 0.5 秒限额")
                        self.outcome = "limited"
                        self.stopped.set()
                        raise Stopped()
        finally:
            stop_process(process)
            if process.stdin:
                process.stdin.close()
            with self.lock:
                self.processes.discard(process)

    def entry(self, entry):
        with self.lock:
            self._entry(entry)

    def _entry(self, entry):
        self.check()
        size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
        if self.counts["entries"] >= self.entry_limit or self.payload_bytes + size > self.byte_limit:
            self.outcome = "limited"
            self.stopped.set()
            raise Stopped()
        self.payload_bytes += size
        self.counts["entries"] += 1
        self.counts["truncated_entries"] += int(entry["preview_truncated"])
        self.emit("entry", entry=entry)
        # Conservative: do not scan the remainder while presenting the cap as complete.
        if self.counts["entries"] >= self.entry_limit:
            self.outcome = "limited"
            self.stopped.set()
            raise Stopped()


def identity(path):
    info = path.lstat()
    return dict(dev=info.st_dev, inode=info.st_ino, size=info.st_size, mtime_ns=info.st_mtime_ns)


def no_links(path):
    return not any(part.is_symlink() for part in (path,) + tuple(path.parents))


def log_directories(scope, runtime):
    if scope.get("root"):
        root = Path(scope["root"])
        if not no_links(root) or not root.is_dir():
            raise ValueError("部署根目录不存在或包含符号链接")
        result = []
        for service in sorted(root.iterdir()):
            runtime.check()
            logs = service / "logs"
            if service.is_symlink() or logs.is_symlink():
                runtime.increment("skipped_links")
                runtime.warning("symlink", "跳过符号链接", logs)
            elif service.is_dir() and logs.is_dir():
                result.append(logs)
        if scope.get("log_dirs"):
            # 任意子目录都可指（不限于 <service>/logs）；存在性、绝对路径、
            # 是否位于 root 内由 validate() 与这里的目录/符号链接检查兜住。
            selected = []
            for name in scope["log_dirs"]:
                path = Path(name)
                if not no_links(path) or not path.is_dir():
                    raise ValueError("所选日志目录不存在或包含符号链接: " + str(path))
                selected.append(path)
            result = selected
    else:
        result = []
        for name in scope["log_dirs"]:
            path = Path(name)
            if not no_links(path) or not path.is_dir():
                raise ValueError("日志目录不存在或包含符号链接: " + str(path))
            result.append(path)
    # Remove duplicate and nested scopes to prevent duplicate evidence.
    directories = []
    for path in sorted(set(p.absolute() for p in result), key=lambda p: (len(p.parts), str(p))):
        if not any(parent == path or parent in path.parents for parent in directories):
            directories.append(path)
    return directories


def picker_directories(scope, runtime):
    """Pick-list candidates: root's direct subdirectories, preferring <child>/logs."""
    root = Path(scope["root"])
    if not no_links(root) or not root.is_dir():
        raise ValueError("部署根目录不存在或包含符号链接")
    result = []
    for child in sorted(root.iterdir()):
        runtime.check()
        logs = child / "logs"
        if child.is_symlink() or logs.is_symlink():
            runtime.increment("skipped_links")
            runtime.warning("symlink", "跳过符号链接", child)
        elif logs.is_dir():
            result.append(logs)
        elif child.is_dir() and child.name == "logs":
            # root 本身就是服务目录时，它自己的 logs 就是候选；其它子目录靠手输。
            result.append(child)
    return result


def list_logs(directories, runtime):
    """Read-only listing for the directory picker; counts only, no file bodies."""
    result = []
    for directory in directories:
        files, total, stack = 0, 0, [directory]
        while stack:
            runtime.check()
            current = stack.pop()
            try:
                with os.scandir(current) as children:
                    for item in children:
                        runtime.check()
                        if item.is_dir(follow_symlinks=False):
                            stack.append(Path(item.path))
                        elif item.is_file(follow_symlinks=False):
                            files += 1
                            try:
                                total += item.stat(follow_symlinks=False).st_size
                            except OSError:
                                pass
            except OSError as exc:
                runtime.warning("read_error", str(exc), current)
        result.append(dict(service=directory.parent.name if directory.name == "logs" else directory.name,
                           path=str(directory), files=files, bytes=total))
    runtime.emit("logs", directories=[item for item in result if item["files"]])


def discover(directories, runtime):
    for directory in directories:
        stack = [directory]
        while stack:
            runtime.check()
            current = stack.pop()
            try:
                with os.scandir(current) as children:
                    for item in children:
                        runtime.check()
                        if item.is_symlink():
                            runtime.increment("skipped_links")
                            runtime.warning("symlink", "跳过符号链接", item.path)
                        elif item.is_dir(follow_symlinks=False):
                            stack.append(Path(item.path))
                        elif item.is_file(follow_symlinks=False):
                            runtime.increment("discovered")
                            yield Path(item.path), directory
            except OSError as exc:
                runtime.increment("unreadable_files")
                runtime.warning("read_error", str(exc), current)


def open_regular(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError("不是普通文件")
    return os.fdopen(descriptor, "rb")


def file_kind(path):
    with open_regular(path) as stream:
        magic = stream.read(6)
    if magic.startswith(b"\x1f\x8b"):
        return "gzip"
    if magic.startswith((b"BZh", b"\xfd7zXZ", b"PK\x03\x04", b"\x28\xb5\x2f\xfd", b"7z\xbc\xaf\x27\x1c")) or path.suffix.lower() in (".zip", ".xz", ".bz2", ".zst", ".7z"):
        return "unsupported"
    return "plain"


def stop_process(process):
    if process is None:
        return
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)


def candidates(batch, runtime):
    """Only paths cross the pipe; rg never emits potentially huge matching lines."""
    started = time.monotonic()
    args = ["rg", "--no-config", "--encoding", "none", "--no-mmap", "--threads", "1", "-uuu", "-a", "-F", "-l", "-0", "-e", runtime.request["query"], "--"] + [str(path) for path, _ in batch]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    runtime.process = process
    mapping = {os.fsencode(str(path)): (path, directory) for path, directory in batch}
    buffer = b""
    errors = bytearray()
    found = []
    try:
        outputs = [process.stdout, process.stderr]
        while outputs:
            runtime.progress()
            ready, _, _ = select.select(outputs, [], [], .05)
            for output in ready:
                block = os.read(output.fileno(), CHUNK)
                if not block:
                    outputs.remove(output)
                elif output is process.stderr:
                    if len(errors) < 8192:
                        errors.extend(block[:8192 - len(errors)])
                else:
                    buffer += block
                    while b"\0" in buffer:
                        name, buffer = buffer.split(b"\0", 1)
                        if name in mapping:
                            found.append(mapping[name])
                    if len(buffer) > MAX_MESSAGE:
                        raise ValueError("rg 路径输出超限")
        status = process.wait()
        if status not in (0, 1):
            runtime.increment("unreadable_files")
            runtime.warning("rg_error", clean(errors.decode("utf-8", "replace")))
    finally:
        stop_process(process)
        process.stdout.close()
        process.stderr.close()
        runtime.process = None
        runtime.phase("file_candidates", None, started, files_count=len(batch))
    yield from found


def gzip_head(path, runtime):
    """Read at most one output chunk with interruptible native decompression."""
    process = subprocess.Popen(["gzip", "-cd", "--", str(path)], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    runtime.process = process
    prefix = bytearray()
    try:
        while len(prefix) < CHUNK:
            runtime.check()
            ready, _, _ = select.select([process.stdout], [], [], .05)
            if ready:
                block = os.read(process.stdout.fileno(), CHUNK - len(prefix))
                if not block:
                    break
                prefix.extend(block)
        return bytes(prefix)
    finally:
        stop_process(process)
        process.stdout.close()
        runtime.process = None


class Line:
    """Bounded physical line; matching includes bytes omitted from its preview."""
    def __init__(self, query, limit):
        self.query = query
        self.limit = limit
        self.head = bytearray()
        self.tail = b""
        self.match_excerpt = None
        self.size = 0
        self.hit = False
        self.sensitive = False
        self.secret_tail = b""
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.decoding_error = False

    def add(self, block):
        probe = self.secret_tail + block
        if re.search(rb"(?i)(authorization|cookie|token|password|secret|api[-_]?key)", probe):
            self.sensitive = True
        self.secret_tail = probe[-32:]
        if not self.decoding_error:
            try:
                self.decoder.decode(block, final=False)
            except UnicodeDecodeError:
                self.decoding_error = True
        combined = self.tail + block
        index = combined.find(self.query) if self.query else -1
        if index >= 0:
            self.hit = True
            if self.match_excerpt is None:
                self.match_excerpt = combined[max(0, index - 120):index + len(self.query) + 240]
        if len(self.head) < self.limit:
            self.head.extend(block[:self.limit - len(self.head)])
        self.size += len(block)
        overlap = max(0, len(self.query) - 1)
        self.tail = combined[-overlap:] if overlap else b""

    def text(self):
        content = bytes(self.head).rstrip(b"\r")
        truncated = self.size > len(self.head)
        text = content.decode("utf-8", "replace")
        if truncated:
            # Do not expose an excerpt from a sensitive line without its key.
            excerpt = self.match_excerpt if not self.sensitive else None
            text += " …[单行已截断]"
            if excerpt:
                text += " [命中片段] " + excerpt.decode("utf-8", "replace")
        return text, truncated


class LightLine:
    """Bounded raw prefix, without per-line preview/security regex work."""
    def __init__(self, limit):
        self.head = bytearray()
        self.limit = limit
        self.size = 0
        self.hit = False
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.decoding_error = False

    def add(self, block):
        if len(self.head) < self.limit:
            self.head.extend(block[:self.limit - len(self.head)])
        self.size += len(block)
        if not self.decoding_error:
            try:
                self.decoder.decode(block, final=False)
            except UnicodeDecodeError:
                self.decoding_error = True

    def materialize(self, query):
        line = Line(query, self.limit)
        line.add(bytes(self.head))
        line.size = self.size
        return line


def physical_lines(stream, query, runtime, limit=4096, hits=None, lightweight=False):
    def new_line(number):
        return LightLine(limit) if lightweight and number not in hits else Line(query, limit)

    current = new_line(1)
    number = 1
    entry_hash = hashlib.sha256()
    fresh_hash = hashlib.sha256()
    entry_bytes = 0

    def finish(terminated):
        nonlocal entry_hash, fresh_hash, entry_bytes
        is_head = HEAD.match(bytes(current.head)) is not None
        current.is_head = is_head
        if is_head:
            entry_hash = fresh_hash
            entry_bytes = current.size + int(terminated)
        else:
            entry_bytes += current.size + int(terminated)
        current.content_hash = entry_hash.hexdigest()
        current.raw_bytes = entry_bytes
        current.line_hash = fresh_hash.hexdigest()
        current.line_bytes = current.size + int(terminated)
        fresh_hash = hashlib.sha256()
        try:
            current.decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            current.decoding_error = True

    while True:
        runtime.progress()
        block = stream.read(CHUNK)
        if not block:
            break
        runtime.increment("bytes_read", len(block))
        pieces = block.split(b"\n")
        for index, piece in enumerate(pieces):
            runtime.check()
            current.add(piece)
            entry_hash.update(piece)
            fresh_hash.update(piece)
            if index < len(pieces) - 1:
                entry_hash.update(b"\n")
                fresh_hash.update(b"\n")
                finish(True)
                yield number, current
                number += 1
                current = new_line(number)
    if current.size:
        finish(False)
        yield number, current


class Entry:
    def __init__(self, number, line, byte_limit=16384, line_limit=150):
        self.start = number
        self.end = number
        self.preview = []
        self.bytes = 0
        self.byte_limit = byte_limit
        self.line_limit = line_limit
        self.truncated = False
        self.matches = []
        self.matched = False
        self.match_preview = None
        self.total_lines = 0
        self.mask_remaining = False
        self.content_hash = None
        self.raw_bytes = 0
        self.capture_complete = False
        match = HEAD.match(bytes(line.head))
        raw = match.group(1).decode("ascii").replace("T", " ").replace(",", ".") if match else None
        try:
            self.timestamp = parse_time(raw)
        except ValueError:
            self.timestamp = None
        self.offset = match.group(2).decode("ascii") if match and match.group(2) else None
        self.level = "UNKNOWN"
        self.add(number, line)

    def add(self, number, line):
        self.content_hash = line.content_hash
        self.raw_bytes = line.raw_bytes
        self.end = number
        self.total_lines += 1
        text, short = line.text()
        if self.mask_remaining:
            text = "[REDACTED: 多行敏感字段之后的内容]"
        sensitive = SECRET.search(bytes(line.head).decode("utf-8", "replace")) if line.sensitive else None
        if sensitive and sensitive.group(2).strip() in ("", '"', "'", "[", "{"):
            # A value on a following line cannot safely be displayed without its key.
            self.mask_remaining = True
        if self.total_lines == 1:
            match = LEVEL.search(text)
            if match:
                self.level = "WARN" if match.group(1) == "WARNING" else match.group(1)
        if line.hit:
            self.matched = True
            if len(self.matches) < 100:
                self.matches.append(number)
            if self.match_preview is None:
                self.match_preview = text
        size = len(text.encode("utf-8"))
        if not self.preview and size > self.byte_limit:
            self.preview.append(text.encode("utf-8")[:self.byte_limit].decode("utf-8", "replace"))
            self.bytes = self.byte_limit
            self.truncated = True
        elif len(self.preview) < self.line_limit and self.bytes + size <= self.byte_limit:
            self.preview.append(text)
            self.bytes += size
        else:
            self.truncated = True
        self.truncated = self.truncated or short

    def view(self):
        lines = list(self.preview)
        if self.truncated:
            lines.append("……[条目预览已截断，共 {} 个物理行]".format(self.total_lines))
            if self.match_preview and self.match_preview not in lines:
                lines.append("[命中行] " + self.match_preview)
        return [redact(line) for line in lines]


def parsed_entries(path, kind, query, runtime, byte_limit=16384, line_limit=150, hits=None, before=None):
    max_hit = max(hits) if hits else None
    with open_regular(path) as raw:
        stream = gzip.GzipFile(fileobj=raw) if kind == "gzip" else raw
        try:
            entry = None
            pending = []
            start = 1
            total = 0
            last = None
            for number, line in physical_lines(stream, query, runtime, min(byte_limit // 2, 65536), hits, hits is not None):
                if line.decoding_error:
                    runtime.increment("decoding_errors")
                if total == 0 or line.is_head:
                    closed = entry is not None
                    if closed:
                        entry.capture_complete = True
                        yield entry
                    entry = None
                    pending = []
                    start = number
                    total = 0
                    # Past the last requested hit the file tail holds no entry we need.
                    if closed and hits is not None and number > max_hit:
                        break
                total += 1
                last = line
                if hits is None:
                    if entry is None:
                        entry = Entry(number, line, byte_limit, line_limit)
                    else:
                        entry.add(number, line)
                    continue
                if entry is not None:
                    if isinstance(line, LightLine):
                        detailed = line.materialize(query)
                        detailed.content_hash, detailed.raw_bytes = line.content_hash, line.raw_bytes
                        line = detailed
                    entry.add(number, line)
                elif number in hits:
                    for old_number, old in pending:
                        detailed = old.materialize(query)
                        detailed.content_hash, detailed.raw_bytes = old.content_hash, old.raw_bytes
                        if entry is None:
                            entry = Entry(old_number, detailed, byte_limit, line_limit)
                        else:
                            entry.add(old_number, detailed)
                    if entry is None:
                        entry = Entry(number, line, byte_limit, line_limit)
                    else:
                        entry.add(number, line)
                    entry.start = start
                    entry.total_lines = total
                    entry.truncated = entry.truncated or total > len(pending) + 1
                    pending = []
                else:
                    if len(pending) < line_limit:
                        pending.append((number, line))
                    else:
                        # Physical count is not preview admission: large lines
                        # may be skipped while later small lines still fit.
                        # Fall back for this entry rather than drop any state.
                        for old_number, old in pending + [(number, line)]:
                            detailed = old.materialize(query)
                            detailed.content_hash, detailed.raw_bytes = old.content_hash, old.raw_bytes
                            if entry is None:
                                entry = Entry(old_number, detailed, byte_limit, line_limit)
                            else:
                                entry.add(old_number, detailed)
                        pending = []
            if entry is not None:
                entry.content_hash, entry.raw_bytes = last.content_hash, last.raw_bytes
                entry.capture_complete = before is None or identity(path) == before
                yield entry
        finally:
            if stream is not raw:
                stream.close()


def display_path(path, runtime):
    if runtime.request["scope"].get("root"):
        return str(path.relative_to(runtime.request["scope"]["root"]))
    return str(path)


def filename_day(path):
    match = re.search(r"\d{4}-\d\d-\d\d", path.name)
    if not match:
        return None
    try:
        return datetime.datetime.strptime(match.group(0), "%Y-%m-%d")
    except ValueError:
        return None


def day_excluded(day, since, until):
    """Same filename-date rule for search and exception-stats."""
    if day is None:
        return False
    end = day + datetime.timedelta(days=1) if day.year < 9999 or day.month < 12 or day.day < 31 else datetime.datetime.max
    return (since is not None and end <= since) or (until is not None and day >= until)


def record(entry, path, directory, before, runtime):
    display = display_path(path, runtime)
    key = "{}:{}:{}:{}".format(path, before["inode"], before["mtime_ns"], entry.start)
    return dict(id=hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()[:24], ts=entry.timestamp.isoformat(" ") if entry.timestamp else None, time_offset=entry.offset, service=directory.parent.name if directory.name == "logs" else directory.name, level=entry.level, file=display, start_line=entry.start, end_line=entry.end, match_lines=entry.matches, preview=entry.view(), preview_truncated=entry.truncated, source_identity=before, source_changed=False, time_unknown=entry.timestamp is None, content_hash=entry.content_hash, raw_bytes=entry.raw_bytes, capture_complete=entry.capture_complete, content_kind="entry")


CONTEXT_LINES = 32
CONTEXT_AFTER = 16
ROW_CAP = 16384
SEGMENT_LINES = 2000
SEGMENT_BYTES = 4 * 1024 * 1024


def hit_rows(path, kind, runtime, before):
    """One native pass per file: rg prints hits with bounded context, Python never re-reads by default.

    Line mode yields the hit lines themselves; context above supplies the entry header
    (timestamp/level) and multi-line-secret masking, exception hits fall back to a re-read.
    Entry mode accumulates each head..next-head segment and assembles it when the window
    proves the entry is complete; anything else (long stacks, capped lines, EOF-open entry)
    falls back to the parsed_entries re-read.
    """
    query = runtime.request["query"].encode("utf-8")
    want_entries = runtime.request.get("result_mode", "lines") == "entries"
    auto = runtime.request.get("auto_expand_exceptions", True)
    group = []            # line mode: (number, raw, capped, is_hit) context since the last separator
    segment = None        # entry mode: open head..next-head records, or None before the first head
    segment_bytes = 0
    segment_overflow = False
    pending = []          # line mode: entries held back until expansion coverage is known
    assembled = []        # entry mode: completed entries awaiting backpressured drain
    expanded = set()      # line mode: exception / pattern hits taken by the re-read pass
    rereread = set()      # entry mode hits whose window was insufficient
    pending_bytes = 0
    counted = False
    budget_hit = False
    unzip = None
    process = None
    errors = bytearray()

    def secret_start(raw):
        if b":" not in raw and b"=" not in raw:
            return False
        if not any(word in raw.lower() for word in SECRET_WORDS):
            return False
        found = SECRET.search(raw.decode("utf-8", "replace"))
        return bool(found and found.group(2).strip() in ("", '"', "'", "[", "{"))

    def make_line(raw, is_hit):
        line = Line(query, 8192)
        line.add(raw)
        line.hit = line.hit or is_hit  # rg matched even when the match sits past the capped prefix
        line.content_hash = hashlib.sha256(bytes(raw) + b"\n").hexdigest()
        line.raw_bytes = len(raw) + 1
        return line

    def assemble(records):
        """Build the full entry from head..next-head lines; hashes match the streaming parser."""
        entry = None
        digest = hashlib.sha256()
        total = 0
        for number, raw, capped, is_hit in records:
            line = make_line(raw, is_hit)
            if entry is None:
                entry = Entry(number, line)
            else:
                entry.add(number, line)
            digest.update(bytes(raw) + b"\n")
            total += len(raw) + 1
        entry.content_hash = digest.hexdigest()
        entry.raw_bytes = total
        entry.capture_complete = identity(path) == before
        return entry

    def close_segment(bounded):
        """bounded=True only when a following header proves the entry ended here."""
        nonlocal segment, segment_bytes, segment_overflow
        if segment is None:
            return
        hits = [record for record in segment if record[3]]
        if hits and bounded and not segment_overflow and not any(record[2] for record in segment):
            assembled.append(assemble(segment))
        else:
            for record in hits:
                rereread.add(record[0])
        segment, segment_bytes, segment_overflow = None, 0, False

    def hit_budget():
        return len(pending) + len(expanded) + len(assembled) + len(rereread)

    def over_budget():
        nonlocal budget_hit
        if hit_budget() > runtime.entry_limit:
            budget_hit = True

    def handle(record_line, capped):
        nonlocal counted, pending_bytes, budget_hit, segment, segment_bytes, segment_overflow
        match = re.match(rb"^(\d+)([:\-])(.*)", record_line, re.S)
        if not match:
            if want_entries:
                close_segment(False)  # separator: the entry may continue beyond the window
            else:
                group.clear()
            return
        number = int(match.group(1))
        raw = bytes(match.group(3))
        is_hit = match.group(2) == b":"
        record = (number, raw[:ROW_CAP], capped, is_hit)
        if want_entries:
            is_head = HEAD.match(raw[:8192]) is not None
            if is_head:
                close_segment(True)
            if segment is None and not is_head:
                if is_hit:
                    rereread.add(number)  # continuation with no visible header: window cannot bound it
                    over_budget()
                return
            if segment is None:
                segment, segment_bytes, segment_overflow = [], 0, False
            segment.append(record)
            segment_bytes += len(record[1]) + 1
            if is_hit and not counted:
                runtime.increment("candidates")
                counted = True
            if segment_bytes > SEGMENT_BYTES or len(segment) > SEGMENT_LINES:
                segment_overflow = True  # keep tracking hits, but this entry needs the re-read pass
            over_budget()
            return
        group.append(record)
        del group[:-CONTEXT_LINES - CONTEXT_AFTER - 4]
        if not is_hit:
            return
        if not counted:
            runtime.increment("candidates")
            counted = True
        line = make_line(raw, True)
        shown, _ = line.text()
        if (auto and EXCEPTION.search(shown)) or runtime.expansion_matches(shown):
            expanded.add(number)
            over_budget()
            return
        entry = Entry(number, line)
        entry.content_kind = "line"
        entry.capture_complete = identity(path) == before
        before_hit = group[:-1]
        head_index = next((index for index in range(len(before_hit) - 1, -1, -1) if HEAD.match(before_hit[index][1])), None)
        segment_lines = before_hit[head_index:] if head_index is not None else before_hit
        if entry.timestamp is None and head_index is not None:
            head_line = before_hit[head_index][1]
            found = HEAD.match(head_line)
            try:
                timestamp = parse_time(found.group(1).decode("ascii")) if found else None
            except ValueError:
                timestamp = None
            offset = found.group(2).decode("ascii") if found and found.group(2) else None
            level = LEVEL.search(head_line.decode("utf-8", "replace"))
            entry.timestamp, entry.offset, entry.level = timestamp, offset, "WARN" if level and level.group(1) == "WARNING" else (level.group(1) if level else "UNKNOWN")
        if any(secret_start(item[1]) for item in segment_lines):
            # A secret block opened above the hit masks it; the hit's own key is redacted inline.
            entry.preview = ["[REDACTED: 多行敏感字段之后的内容]"]
            entry.match_preview = None
        if capped:
            entry.truncated = True  # never aggregate a line whose bytes we did not fully see
        pending.append(entry)
        pending_bytes += len(json.dumps(entry.view(), ensure_ascii=False).encode("utf-8")) + len(os.fsencode(str(path))) * 6 + 2048
        if pending_bytes > runtime.byte_limit:
            budget_hit = True
        over_budget()

    try:
        args = ["rg", "--no-config", "--no-mmap", "--threads", "1", "-a", "--encoding", "none",
                "--color", "never", "--no-heading", "-n", "-F", "-B", str(CONTEXT_LINES), "-e", runtime.request["query"]]
        if want_entries:
            args[-2:-2] = ["-A", str(CONTEXT_AFTER)]
        if kind == "gzip":
            unzip = subprocess.Popen(["gzip", "-cd", "--", str(path)], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True)
            with runtime.lock:
                runtime.processes.add(unzip)
        else:
            args.extend(["--", str(path)])
        process = subprocess.Popen(args, stdin=unzip.stdout if unzip else None,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        if unzip:
            unzip.stdout.close()
        runtime.process = process
        outputs = [process.stdout, process.stderr] + ([unzip.stderr] if unzip else [])
        buffer = b""
        dropping = False
        while outputs and not budget_hit:
            runtime.progress()
            runtime.check()
            ready, _, _ = select.select(outputs, [], [], .05)
            for output in ready:
                block = os.read(output.fileno(), CHUNK)
                if not block:
                    outputs.remove(output)
                    continue
                if output is not process.stdout:
                    if len(errors) < 8192:
                        errors.extend(block[:8192 - len(errors)])
                    continue
                buffer += block
                while True:
                    if dropping:
                        cut = buffer.find(b"\n")
                        if cut < 0:
                            buffer = b""
                            break
                        buffer = buffer[cut + 1:]
                        dropping = False
                    cut = buffer.find(b"\n")
                    if cut >= 0:
                        record_line, buffer = buffer[:cut], buffer[cut + 1:]
                        handle(record_line, False)
                    elif len(buffer) > ROW_CAP:
                        record_line, buffer, dropping = buffer[:ROW_CAP], buffer[ROW_CAP:], True
                        handle(record_line, True)
                    else:
                        break
                    if budget_hit:
                        break
                while assembled:
                    yield assembled.pop(0)
                    if budget_hit:
                        break
        if buffer and not budget_hit and not dropping:
            handle(buffer, False)
        if want_entries:
            close_segment(False)  # EOF: no following header seen, this entry cannot be bounded
        while assembled and not budget_hit:
            yield assembled.pop(0)
        if budget_hit:
            # 缓冲越界只代表这个文件不再继续读，不代表本次结果作废：已抓到的命中必须照常走
            # 下面的重读并发出，否则高频词查询会得到 0 条。停止时机由 _entry 的配额检查决定。
            for buffered in pending + assembled:
                yield buffered
            del pending[:]
            del assembled[:]
            runtime.outcome = "limited"
        else:
            status = process.wait()
            gzip_status = unzip.wait() if unzip else 0
            if status not in (0, 1) or gzip_status != 0:
                runtime.unreadable(path)
                runtime.warning("gzip_error" if unzip else "rg_error", clean(bytes(errors).decode("utf-8", "replace")), path)
        extract = set(rereread) if want_entries else set(expanded)
        covering = []
        if extract:
            extract_started = time.monotonic()
            read_before = getattr(runtime.local, "bytes_read", 0)
            try:
                covering = [entry for entry in parsed_entries(path, kind, query, runtime, hits=extract, before=before)
                            if want_entries or any(entry.start <= hit <= entry.end for hit in extract)]
            finally:
                # Terminal events must still see the extract phase when the re-read aborts.
                runtime.phase("extract", path, extract_started, re_read=len(extract),
                              bytes_read=getattr(runtime.local, "bytes_read", 0) - read_before)
        if want_entries:
            yield from covering
        else:
            for buffered in pending:
                if not any(entry.start <= buffered.start <= entry.end for entry in covering):
                    yield buffered
            yield from covering
    finally:
        stop_process(process)
        stop_process(unzip)
        if process:
            process.stdout.close()
            process.stderr.close()
        if unzip:
            unzip.stderr.close()
            with runtime.lock:
                runtime.processes.discard(unzip)
        runtime.process = None


def search_file(path, directory, kind, runtime):
    before = identity(path)
    started = time.monotonic()
    since, until = parse_time(runtime.request.get("since")), parse_time(runtime.request.get("until"))
    mode = "native-entries" if runtime.request.get("result_mode", "lines") == "entries" else "native-lines"
    try:
        for entry in hit_rows(path, kind, runtime, before):
            if entry.timestamp is None:
                runtime.increment("time_unknown")
                if (since is not None or until is not None) and not runtime.request.get("include_unknown_time", False):
                    continue
            elif (since is not None and entry.timestamp < since) or (until is not None and entry.timestamp >= until):
                continue
            result = record(entry, path, directory, before, runtime)
            result["content_kind"] = getattr(entry, "content_kind", "entry")
            runtime.entry(result)
    finally:
        runtime.phase("locate", path, started, mode=mode)
    after = identity(path)
    if before != after:
        runtime.increment("source_changed")
        runtime.warning("source_changed", "扫描期间日志发生变化", path)
    runtime.increment("scanned")


ROTATION = re.compile(r"^(.*)\.(\d+)(\.gz)?$")


def family_prune(files, since, until, runtime):
    """Drop rotation members whose time interval cannot meet [since, until).

    Rotation order is the sequence number; member interval starts where the previous
    rotation stopped. mtime (content end) prunes the left side for free; the right
    side needs head timestamps, so probe with cached bisection (log N reads).
    Any doubt — short group, mtime contradicting sequence, unparseable head on the
    probe path, stat error — keeps the whole family: slow beats wrong.
    """
    groups = {}
    for path, directory in files:
        match = ROTATION.match(path.name)
        if match:
            groups.setdefault((str(path.parent), match.group(1)), []).append((path, directory, int(match.group(2))))
    dropped = set()
    heads = {}

    def head_time(path):
        if path not in heads:
            value = None
            try:
                if path.name.endswith(".gz"):
                    prefix = gzip_head(path, runtime)
                else:
                    with open_regular(path) as raw:
                        prefix = raw.read(CHUNK)
                for line in prefix.split(b"\n"):
                    match = HEAD.match(line[:8192])
                    if match:
                        try:
                            value = parse_time(match.group(1).decode("ascii"))
                        except ValueError:
                            value = None
                        break
            except (OSError, EOFError, zlib.error):
                value = None
            heads[path] = value
        return heads[path]

    for members in groups.values():
        if len(members) < 3:
            continue
        members.sort(key=lambda item: item[2])
        try:
            mtimes = [path.stat().st_mtime for path, _, _ in members]
        except OSError:
            continue
        if any(after < before for before, after in zip(mtimes, mtimes[1:])):
            continue
        start = bisect.bisect_left(mtimes, since.timestamp()) if since is not None else 0
        stop = len(members) - 1
        if until is not None:
            low, high = start, len(members)
            undecided = False
            while low < high:
                middle = (low + high) // 2
                head = head_time(members[middle][0])
                if head is None:
                    undecided = True
                    break
                if head >= until:
                    high = middle
                else:
                    low = middle + 1
            if not undecided:
                stop = low - 1
        if start == 0 and stop == len(members) - 1:
            continue
        for index, (path, _, _) in enumerate(members):
            if start <= index <= stop:
                continue
            dropped.add(str(path))
            runtime.increment("skipped_time_files")
            runtime.phase("prefilter", path, time.monotonic(), decision="exclude",
                          reason="family_mtime_before_window" if index < start else "family_head_after_window", bytes_read=0)
    return [(path, directory) for path, directory in files if str(path) not in dropped]


def run_search(directories, runtime):
    planned = []
    since, until = parse_time(runtime.request.get("since")), parse_time(runtime.request.get("until"))
    listed = list(discover(directories, runtime))
    if (since is not None or until is not None) and runtime.request.get("filter_file_dates", True):
        listed = family_prune(listed, since, until, runtime)
    for path, directory in listed:
        started = time.monotonic()
        try:
            kind = file_kind(path)
            if kind == "gzip" and not runtime.request.get("include_archives", True):
                continue
            day = filename_day(path)
            filename_date = day.strftime("%Y-%m-%d") if day is not None else None
            excluded = runtime.request.get("filter_file_dates", True) and day_excluded(day, since, until)
            if excluded:
                runtime.increment("skipped_time_files")
                runtime.phase("prefilter", path, started, filename_date=filename_date,
                              header_time=None, decision="exclude", reason="filename_day_outside_window", bytes_read=0)
                continue
            header_time = None
            read_bytes = 0
            if kind != "unsupported":
                try:
                    if kind == "gzip":
                        prefix = gzip_head(path, runtime)
                    else:
                        with open_regular(path) as raw:
                            prefix = raw.read(CHUNK)
                    read_bytes = len(prefix)
                    for line in prefix.split(b"\n"):
                        match = HEAD.match(line[:8192])
                        if match:
                            try:
                                header_time = parse_time(match.group(1).decode("ascii"))
                            except ValueError:
                                pass
                            break
                except (OSError, EOFError, zlib.error):
                    # A bounded probe is not an archive validation or an exclusion.
                    pass
            in_window = header_time is not None and (since is None or header_time >= since) and (until is None or header_time < until)
            runtime.phase("prefilter", path, started, filename_date=filename_date,
                          header_time=header_time.isoformat(" ") if header_time else None,
                          decision="include" if runtime.request.get("filter_file_dates", True) else "retain", reason="filename_day_intersects_window" if day is not None and runtime.request.get("filter_file_dates", True) else "unknown_date_or_filter_disabled_header_priority_only",
                          bytes_read=read_bytes)
            day_end = day + datetime.timedelta(days=1) if day is not None and day < datetime.datetime(9999, 12, 31) else datetime.datetime.max
            dated_in_window = day is not None and (since is None or day_end > since) and (until is None or day < until)
            # Dated rotations are usually stable. Keep active/unknown names
            # behind them even when an active file's first header is in range.
            planned.append((not dated_in_window, not in_window, filename_date or "", str(path), path, directory, kind))
        except OSError as exc:
            runtime.unreadable(path)
            runtime.warning("read_error", str(exc), path)
    def scan(item):
        _, _, _, _, path, directory, kind = item
        runtime.check()
        try:
            if kind == "unsupported":
                runtime.increment("unsupported_files")
                runtime.warning("unsupported", "仅支持普通文件和 gzip", path)
            else:
                search_file(path, directory, kind, runtime)
        except (OSError, EOFError, zlib.error) as exc:
            runtime.unreadable(path)
            runtime.warning("read_error", str(exc), path)

    # A shared iterator gives each worker its next file only after extraction ends.
    tasks = iter(sorted(planned))
    task_lock = threading.Lock()

    def consume():
        try:
            while True:
                runtime.check()
                with task_lock:
                    item = next(tasks, None)
                if item is None:
                    return
                scan(item)
        except BaseException:
            runtime.stopped.set()
            raise

    if planned:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, runtime.request.get("concurrency", 2), len(planned))) as executor:
            futures = [executor.submit(consume) for _ in range(min(2, runtime.request.get("concurrency", 2), len(planned)))]
            failures = []
            for future in futures:
                try:
                    future.result()
                except BaseException as exc:
                    failures.append(exc)
            if failures:
                raise next((exc for exc in failures if not isinstance(exc, Stopped)), failures[0])


def context(directories, runtime):
    request = runtime.request
    name = Path(request["file"])
    if not name.is_absolute():
        if not request["scope"].get("root"):
            raise ValueError("指定日志目录模式需要绝对文件路径")
        name = Path(request["scope"]["root"]) / name
    name = name.absolute()
    directory = next((path for path in directories if path in name.parents), None)
    if directory is None or not no_links(name):
        raise ValueError("上下文路径超出日志范围或包含符号链接")
    before = identity(name)
    if request.get("source_identity") and request["source_identity"] != before:
        runtime.warning("source_changed", "来源文件已变化，请重新搜索", name)
        runtime.increment("source_changed")
        return
    kind = file_kind(name)
    if kind == "unsupported":
        raise ValueError("不支持此压缩格式")
    previous = collections.deque(maxlen=2)
    results = []
    remaining = None
    total_bytes = 0
    target = request["line"]
    for entry in parsed_entries(name, kind, b"", runtime, 65536, 500, before=before):
        if remaining is None and entry.start <= target <= entry.end:
            results.extend(previous)
            results.append(record(entry, name, directory, before, runtime))
            remaining = 2
        elif remaining is not None:
            results.append(record(entry, name, directory, before, runtime))
            remaining -= 1
            if remaining == 0:
                break
        else:
            previous.append(record(entry, name, directory, before, runtime))
    if remaining is None:
        raise ValueError("行号不在文件中")
    safe_results = []
    truncated = False
    for result in results:
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        if total_bytes + size > 256 * 1024:
            truncated = True
            break
        total_bytes += size
        safe_results.append(result)
    changed = identity(name) != before
    if changed:
        runtime.increment("source_changed")
    runtime.increment("truncated_entries", sum(e["preview_truncated"] for e in safe_results))
    if truncated:
        runtime.increment("truncated_entries")
    runtime.emit("context", entries=safe_results, target_line=target, source_changed=changed, truncated=truncated or any(e["preview_truncated"] for e in safe_results))


AWK_EXCEPTIONS = r"""
BEGIN {
  HEAD  = "^[ \t]*(\\[[^]]{0,256}\\])?[ \t]*[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][ T][0-9][0-9]:[0-9][0-9]:[0-9][0-9]"
  TS    = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][ T][0-9][0-9]:[0-9][0-9]:[0-9][0-9]"
  LVL   = "(^|[^A-Za-z0-9_])(ERROR|FATAL)([^A-Za-z0-9_]|$)"
  EXC   = "(^|[^A-Za-z0-9_$.])[A-Za-z0-9_$.]*(Exception|Error)([^A-Za-z0-9_]|$)"
  PYF   = "^[ \t]*File[ \t]+\"[^\"]*\",[ \t]*line[ \t]+[0-9]+,[ \t]+in[ \t]+[^ \t]+[ \t\r]*$"
  SINCE = norm(SINCE); UNTIL = norm(UNTIL)
  total = 0; exceptions = 0; unstacked = 0; unknown = 0; bytes = 0; line = 0; last = 0
}
function norm(value,   frac) {
  if (value == "") return ""
  gsub(/T/, " ", value); gsub(/,/, ".", value)
  if (length(value) <= 19) return substr(value, 1, 19) ".000000"
  frac = substr(value, 21); gsub(/[^0-9]/, "", frac)
  while (length(frac) < 6) frac = frac "0"
  return substr(value, 1, 19) "." substr(frac, 1, 6)
}
function headts(text,   stamp, rest, frac) {
  if (!match(text, TS)) return ""
  stamp = substr(text, RSTART, 19); gsub(/T/, " ", stamp)
  rest = substr(text, RSTART + 19, 7)
  if (substr(rest, 1, 1) == "." || substr(rest, 1, 1) == ",") {
    frac = substr(rest, 2); gsub(/[^0-9]/, "", frac)
    while (length(frac) < 6) frac = frac "0"
    return stamp "." substr(frac, 1, 6)
  }
  return stamp ".000000"
}
function begin(stamp, error) {
  have = 1; estart = line; ets = stamp; eerr = error; eexc = 0; estack = 0; etype = ""; nf = 0; f1 = ""; f2 = ""; f3 = ""
}
function addframe(name) {
  if (nf >= 3) return
  nf++
  if (nf == 1) f1 = name; else if (nf == 2) f2 = name; else f3 = name
}
function frame(text,   name, cut) {
  sub(/^[ \t]+/, "", text)
  if (substr(text, 1, 3) == "at ") {
    sub(/^at[ \t]+/, "", text)
    cut = index(text, "(")
    if (cut > 1) {
      name = substr(text, 1, cut - 1); sub(/[ \t]+$/, "", name)
      if (name ~ /^[A-Za-z0-9_$.<>]+$/) { eexc = 1; estack = 1; addframe(name) }
    }
    return
  }
  if (text ~ PYF) {
    name = text; sub(/^.*[ \t]in[ \t]+/, "", name); sub(/[ \t\r]+$/, "", name)
    eexc = 1; estack = 1; addframe(name)
  }
}
function flush(   key) {
  if (!have) return
  have = 0
  if (ets == "") {
    unknown++
    if (SINCE != "" || UNTIL != "") { if (!UNK) return }
  } else if ((SINCE != "" && ets < SINCE) || (UNTIL != "" && ets >= UNTIL)) return
  total++
  if (!(eerr || eexc)) return
  exceptions++
  if (!estack) { unstacked++; return }
  key = etype "|" f1 "," f2 "," f3
  if (!(key in gcount)) {
    gcount[key] = 0; gtype[key] = etype; gframes[key] = f1 "," f2 "," f3
    gfirst[key] = ets; glast[key] = ets; gstart[key] = estart; gend[key] = last; gts[key] = ets
  }
  gcount[key]++
  if (ets != "") {
    if (gfirst[key] == "" || ets < gfirst[key]) gfirst[key] = ets
    if (glast[key] == "" || ets > glast[key]) glast[key] = ets
  }
}
{
  line++
  bytes += length($0) + 1
  c = substr($0, 1, 1)
  head = 0
  if (c == " " || c == "\t" || c == "[" || (c >= "0" && c <= "9")) {
    text = substr($0, 1, 1024)
    if (text ~ HEAD) { flush(); head = 1; begin(headts(text), (text ~ LVL)) }
  }
  if (!head && line == 1) begin("", ($0 ~ LVL))
  if (index($0, "Exception") || index($0, "Error")) {
    if (match($0, EXC)) {
      eexc = 1; estack = 1
      if (etype == "") {
        etype = substr($0, RSTART, RLENGTH)
        sub(/^[^A-Za-z0-9_$.]/, "", etype); sub(/[^A-Za-z0-9_$.]$/, "", etype)
      }
    }
  } else if (index($0, "Caused by:") || index($0, "Suppressed:") || index($0, "Traceback (")) {
    eexc = 1; estack = 1
  }
  if (c == "\t" && substr($0, 2, 2) == "at" && substr($0, 4, 1) == " ") {
    frame(substr($0, 2, 1024))
  } else if ((c == " " || c == "\t") && (index($0, "at ") || index($0, "File "))) {
    frame(substr($0, 1, 1024))
  }
  last = line
}
END {
  flush()
  printf "T\t%d\t%d\t%d\t%d\t%d\n", total, exceptions, unstacked, unknown, bytes
  for (key in gcount) printf "G\t%d\t%s\t%s\t%d\t%d\t%s\t%s\t%s\n", gcount[key], gfirst[key], glast[key], gstart[key], gend[key], gts[key], gtype[key], gframes[key]
}
"""


def scan_exceptions(path, kind, runtime, since, until):
    """One native pass per file: awk groups the stacks, no log line ever enters Python."""
    source = None
    stream = None
    if kind == "gzip":
        source = subprocess.Popen(["gzip", "-cd", "--", str(path)], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, start_new_session=True)
        with runtime.lock:
            runtime.processes.add(source)
        stream = source.stdout
    else:
        stream = open_regular(path)
    request = runtime.request
    args = ["awk", "-v", "SINCE=" + (since.isoformat(" ") if since else ""),
            "-v", "UNTIL=" + (until.isoformat(" ") if until else ""),
            "-v", "UNK=" + ("1" if request.get("include_unknown_time") else "0"), AWK_EXCEPTIONS]
    process = subprocess.Popen(args, stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, env=dict(os.environ, LC_ALL="C"))
    if source is not None:
        source.stdout.close()
    runtime.process = process
    output, errors = bytearray(), bytearray()
    try:
        streams = [process.stdout, process.stderr]
        while streams:
            runtime.progress()
            ready, _, _ = select.select(streams, [], [], .05)
            for current in ready:
                block = os.read(current.fileno(), CHUNK)
                if not block:
                    streams.remove(current)
                elif current is process.stderr:
                    if len(errors) < 8192:
                        errors.extend(block[:8192 - len(errors)])
                else:
                    output.extend(block)
                    if len(output) > MAX_MESSAGE:
                        raise ValueError("awk 输出超限")
        status = process.wait()
        gzip_status = source.wait() if source is not None else 0
    finally:
        stop_process(process)
        stop_process(source)
        process.stdout.close()
        process.stderr.close()
        if source is not None:
            with runtime.lock:
                runtime.processes.discard(source)
        else:
            stream.close()
        runtime.process = None
    if status != 0:
        raise ValueError("awk 失败: " + clean(errors.decode("utf-8", "replace"))[:200])
    if gzip_status != 0:
        raise EOFError("gzip 解压失败")
    total, exceptions, unstacked, unknown, read_bytes = 0, 0, 0, 0, 0
    rows = []
    for line in output.decode("utf-8", "replace").splitlines():
        fields = line.split("\t")
        if fields[0] == "T" and len(fields) == 6:
            total, exceptions, unstacked, unknown, read_bytes = (int(value) for value in fields[1:])
        elif fields[0] == "G" and len(fields) == 9:
            rows.append(fields[1:])
    return total, exceptions, unstacked, unknown, read_bytes, rows


def exception_stats(directories, runtime):
    """Count entries and aggregate exception stacks in [since, until); awk does the grouping."""
    request = runtime.request
    since = parse_time(request.get("since"))
    until = parse_time(request.get("until"))
    include_unknown_time = request.get("include_unknown_time", False)
    include_archives = request.get("include_archives", True)
    top = request.get("top", 10)
    total_entries = 0
    exception_entries = 0
    unstacked = 0
    groups = {}
    listed = list(discover(directories, runtime))
    if (since is not None or until is not None) and request.get("filter_file_dates", True):
        listed = family_prune(listed, since, until, runtime)
    for path, directory in listed:
        started = time.monotonic()
        try:
            kind = file_kind(path)
        except OSError as exc:
            runtime.unreadable(path)
            runtime.warning("read_error", str(exc), path)
            continue
        if kind == "unsupported":
            runtime.increment("unsupported_files")
            runtime.warning("unsupported", "仅支持普通文件和 gzip", path)
            continue
        if kind == "gzip" and not include_archives:
            continue
        day = filename_day(path)
        if request.get("filter_file_dates", True) and day_excluded(day, since, until):
            runtime.increment("skipped_time_files")
            runtime.phase("prefilter", path, started, filename_date=day.strftime("%Y-%m-%d"),
                          decision="exclude", reason="filename_day_outside_window", bytes_read=0)
            continue
        try:
            before = identity(path)
        except OSError as exc:
            runtime.unreadable(path)
            runtime.warning("read_error", str(exc), path)
            continue
        try:
            total, exceptions, silent, unknown, read_bytes, rows = scan_exceptions(path, kind, runtime, since, until)
        except (OSError, EOFError, ValueError) as exc:
            runtime.unreadable(path)
            runtime.warning("read_error", str(exc), path)
            continue
        runtime.increment("bytes_read", read_bytes)
        runtime.increment("time_unknown", unknown)
        total_entries += total
        exception_entries += exceptions
        unstacked += silent
        for count, first, end_time, start_line, end_line, sample_time, exception_type, frames in rows:
            key_frames = [name for name in frames.split(",") if name]
            key = exception_type + "|" + ",".join(key_frames)
            group = groups.get(key)
            if group is None:
                groups[key] = group = dict(
                    exception_type=exception_type, key_frames=key_frames, count=0,
                    first_seen=None, last_seen=None, sample=None, _first=None, _last=None)
            group["count"] += int(count)
            stamp = parse_time(first)
            if stamp is not None and (group["_first"] is None or stamp < group["_first"]):
                group["_first"] = stamp
                group["first_seen"] = stamp.isoformat(" ")
            stamp = parse_time(end_time)
            if stamp is not None and (group["_last"] is None or stamp > group["_last"]):
                group["_last"] = stamp
                group["last_seen"] = stamp.isoformat(" ")
            if group["sample"] is None:
                stamp = parse_time(sample_time)
                group["sample"] = dict(ts=stamp.isoformat(" ") if stamp else None,
                                       file=display_path(path, runtime),
                                       start_line=int(start_line), end_line=int(end_line))
        runtime.increment("scanned")
        try:
            if before != identity(path):
                runtime.increment("source_changed")
                runtime.warning("source_changed", "扫描期间日志发生变化", path)
        except OSError:
            pass
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["exception_type"], ",".join(g["key_frames"])))
    result_groups = [{k: v for k, v in g.items() if not k.startswith("_")} for g in ordered[:top]]
    runtime.emit("exception_stats",
                 total_entries=total_entries,
                 exception_entries=exception_entries,
                 exception_rate=round(exception_entries / total_entries, 6) if total_entries else 0.0,
                 unstacked_exceptions=unstacked,
                 grouped_exceptions=exception_entries - unstacked,
                 groups_total=len(groups),
                 top=top,
                 groups=result_groups,
                 since=request.get("since"), until=request.get("until"),
                 include_unknown_time=include_unknown_time)


def execute(request, emit, cancelled=None):
    runtime = Runtime(request, emit, cancelled)
    try:
        validate(request)
        scope = request["scope"]
        pick = request.get("action") == "logs" and scope.get("root") and not scope.get("log_dirs")
        directories = picker_directories(scope, runtime) if pick else log_directories(scope, runtime)
        if not directories:
            raise ValueError("未找到日志目录")
        if not shutil.which("rg") and request.get("action", "search") not in ("logs", "exception-stats"):
            raise ValueError("缺少 rg，请在查询目标安装 ripgrep")
        if request.get("action", "search") != "logs" and request.get("include_archives", True) and not shutil.which("gzip"):
            raise ValueError("缺少 gzip，请在查询目标安装 gzip 或使用 --no-archives")
        if request.get("action") == "exception-stats" and not shutil.which("awk"):
            raise ValueError("缺少 awk，请在查询目标安装 awk（gawk/mawk 均可）")
        runtime.emit("ready", protocol_version=PROTOCOL, helper_version=VERSION, worker_pid=os.getpid(), source_hostname=socket.gethostname(), capabilities=dict(gzip=True, rg=True, awk=bool(shutil.which("awk")), python=sys.version.split()[0]), directories=[str(path) for path in directories])
        if request.get("action", "search") == "search":
            run_search(directories, runtime)
        elif request["action"] == "context":
            context(directories, runtime)
        elif request["action"] == "logs":
            list_logs(directories, runtime)
        elif request["action"] == "exception-stats":
            exception_stats(directories, runtime)
    except Stopped:
        if runtime.cancelled.is_set():
            runtime.outcome = "cancelled"
    except (OSError, EOFError, zlib.error, ValueError, TypeError, KeyError) as exc:
        runtime.outcome = "failed"
        runtime.warning("failed", str(exc))
    finally:
        runtime.stop_all()
    counts = runtime.counts
    complete = runtime.outcome == "completed" and not any(counts[key] for key in ("unreadable_files", "unsupported_files", "source_changed", "skipped_links"))
    action = request.get("action", "search")
    filtered = action == "search" and request.get("filter_file_dates", True)
    scope_basis = "selected_files_after_filename_date_filter" if filtered else "all_discovered_files" if action in ("search", "exception-stats") else "context_file" if action == "context" else "log_directory_listing" if action == "logs" else "directory_check"
    runtime.emit("done", outcome=runtime.outcome, scope_complete=complete, scope_basis=scope_basis, filter_file_dates=filtered, result_mode=request.get("result_mode", "lines"), concurrency=request.get("concurrency", 2), content_complete=not bool(counts["truncated_entries"]), elapsed=round(time.monotonic() - runtime.started, 3), **counts)


class Writer:
    def __init__(self, request, cancelled):
        self.request = request
        self.cancelled = cancelled
        self.seq = 0
        self.descriptor = sys.stdout.fileno()
        os.set_blocking(self.descriptor, False)

    def __call__(self, event):
        self.seq += 1
        data = dict(event, protocol=PROTOCOL, task_id=self.request.get("task_id"), request_id=self.request.get("request_id"), seq=self.seq)
        # ensure_ascii preserves arbitrary Unix filenames without invalid UTF-8.
        block = (json.dumps(data, ensure_ascii=True) + "\n").encode("ascii")
        if len(block) > MAX_MESSAGE:
            raise ValueError("协议消息超过 1 MiB")
        end = time.monotonic() + 5
        while block:
            if self.cancelled.is_set() and event["type"] != "done":
                raise Stopped()
            if time.monotonic() >= end:
                self.cancelled.set()
                raise BrokenPipeError("结果接收端没有响应")
            _, ready, _ = select.select([], [self.descriptor], [], .05)
            if ready:
                try:
                    count = os.write(self.descriptor, block)
                    block = block[count:]
                except BlockingIOError:
                    continue


def main():
    cancelled = threading.Event()
    finished = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: cancelled.set())
    signal.signal(signal.SIGINT, lambda *_: cancelled.set())
    raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if len(raw) > MAX_MESSAGE:
        return 1
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            return 1
    except (ValueError, UnicodeError):
        return 1

    os.set_blocking(sys.stdin.fileno(), False)

    def controls():
        pending = bytearray()
        while not finished.is_set():
            block = sys.stdin.buffer.read1(CHUNK)
            if not block:
                readable, _, _ = select.select([sys.stdin], [], [], .05)
                if not readable:
                    continue
                block = sys.stdin.buffer.read1(CHUNK)
                if not block:
                    cancelled.set()
                    return
            pending.extend(block)
            if len(pending) > MAX_MESSAGE:
                cancelled.set()
                return
            while b"\n" in pending:
                message, _, rest = pending.partition(b"\n")
                pending = bytearray(rest)
                try:
                    control = json.loads(message)
                    if control.get("action") == "cancel" and control.get("task_id") == request.get("task_id"):
                        cancelled.set()
                except (ValueError, AttributeError):
                    cancelled.set()
                    return

    reader = threading.Thread(target=controls, daemon=True)
    reader.start()
    try:
        execute(request, Writer(request, cancelled), cancelled)
    except (BrokenPipeError, OSError):
        return 1
    finally:
        finished.set()
        reader.join(timeout=.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())

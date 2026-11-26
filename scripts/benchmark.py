"""Reproducible synthetic smoke benchmark; never clears OS caches or reads live logs."""

import argparse
import asyncio
import gzip
import json
import os
import platform
import random
import tempfile
import time
from pathlib import Path

from log_search.controller import Controller, request


def dataset(root, size_mib):
    generator = random.Random(7)
    rows = []
    prefix = b"2026-01-01 10:00:00.000 INFO [demo-thread] payload="
    for _ in range(1024):
        payload = generator.randbytes(480).hex().encode()
        rows.append((prefix + payload).ljust(1023, b"x") + b"\n")
    block = b"".join(rows)
    stored = 0
    files = 48
    per_file = max(1, size_mib // files)
    for index in range(files):
        service = ("app-beta", "app-alpha", "app-gamma")[index % 3]
        directory = root / service / "logs/history"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (str(index) + (".gz" if index >= 36 else ".fragment"))
        opener = gzip.open if index >= 36 else open
        positive = index in (0, 12, 24, 36, 44)
        with opener(path, "wb") as output:
            for part in range(per_file):
                data = block
                if positive and part == 0:
                    data = block.replace(b"payload=", b"BENCH-TRACE=", 1)
                output.write(data)
        stored += path.stat().st_size
    return dict(files=files, gzip_files=12, logical_mib=per_file * files, stored_mib=round(stored / 1024**2, 2), expected_entries=5)


async def measure(root):
    controller = Controller()
    start = time.monotonic()
    first = None
    peaks = dict(client=0, worker=0)
    task = await controller.start(request(root=str(root), query="BENCH-TRACE", timeout=180))

    async def sample():
        while not task.runner.done():
            for name, pid in (("client", os.getpid()), ("worker", task.process.pid if task.process else None)):
                if pid:
                    try:
                        rss = int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
                        peaks[name] = max(peaks[name], rss)
                    except (OSError, ValueError):
                        pass
            await asyncio.sleep(.05)

    sampler = asyncio.create_task(sample())
    try:
        async for event in task.events():
            if event["type"] == "entry" and first is None:
                first = round(time.monotonic() - start, 3)
        await sampler
        return dict(first_entry_seconds=first, total_seconds=round(time.monotonic() - start, 3), peak_rss_mib={key: round(value / 1024**2, 2) for key, value in peaks.items()}, done=task.done)
    finally:
        await controller.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=3072)
    args = parser.parse_args()
    if not 48 <= args.size_mib <= 6144:
        parser.error("size must be 48..6144 MiB")
    with tempfile.TemporaryDirectory(prefix="log-search-bench-") as directory:
        root = Path(directory)
        details = dataset(root, args.size_mib)
        print(json.dumps(dict(platform=platform.platform(), python=platform.python_version(), cache="warm/unspecified; freshly generated synthetic files", dataset=details, measurement=asyncio.run(measure(root))), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

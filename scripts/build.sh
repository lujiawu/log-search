#!/bin/sh
# Single-file zipapp. The target only needs python3 >= 3.10 plus rg, gzip and ssh.
# All dependencies are pure Python, so there is nothing to compile or extract at runtime.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
stage="$root/build/app"
rm -rf "$root/build" "$root/dist"
mkdir -p "$stage" "$root/dist"
uv pip install --quiet --target "$stage" --python "$root/.venv/bin/python" "$root[tui]"
find "$stage" -type d -name __pycache__ -prune -exec rm -rf '{}' +
python3 -m zipapp "$stage" --compress --output "$root/dist/log-search" \
  --python '/usr/bin/env python3' --main 'log_search.cli:main'
chmod +x "$root/dist/log-search"
# Self-check: the worker must still start from inside the archive, not only from the repo tree.
demo=$(mktemp -d)
mkdir -p "$demo/svc/logs"
printf '2026-01-01 00:00:00 INFO TRACE\n' > "$demo/svc/logs/app.log"
"$root/dist/log-search" search --local --root "$demo" --query TRACE >/dev/null
rm -rf "$demo"
du -h "$root/dist/log-search"

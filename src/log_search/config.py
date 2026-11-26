"""Local preferences and OpenSSH alias discovery; never read identity contents."""

import glob
import json
import os
import shlex
from pathlib import Path


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "log-search" / "config.json"


def load_preferences():
    try:
        value = json.loads(config_path().read_text())
        if not isinstance(value, dict):
            return {}
        roots = value.get("roots", {})
        value["roots"] = {key: path for key, path in roots.items() if isinstance(key, str) and isinstance(path, str)} if isinstance(roots, dict) else {}
        picked = value.get("picked", {})
        def valid(entry):
            return isinstance(entry, dict) and isinstance(entry.get("root"), str) and isinstance(entry.get("dirs"), list) and all(isinstance(path, str) for path in entry["dirs"])
        value["picked"] = {key: entry for key, entry in picked.items() if isinstance(key, str) and valid(entry)} if isinstance(picked, dict) else {}
        if not isinstance(value.get("ssh_config", ""), str):
            value.pop("ssh_config", None)
        return value
    except (OSError, ValueError):
        return {}


def save_preferences(values):
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(values, output, ensure_ascii=False, indent=2)
        output.write("\n")
    temporary.replace(path)


def ssh_config_path(explicit=None):
    return str(Path(explicit or load_preferences().get("ssh_config") or "~/.ssh/config").expanduser())


def host_aliases(filename):
    """Include is relative to ~/.ssh, as in OpenSSH, not the including directory."""
    found = set()
    visited = set()

    def scan(path, depth=0):
        path = Path(path).expanduser().absolute()
        if path in visited or depth > 16:
            return
        visited.add(path)
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            if depth == 0:
                raise ValueError("SSH config 不存在: " + str(path))
            return
        except (OSError, UnicodeError) as exc:
            raise ValueError("无法读取 SSH config: " + str(exc)) from exc
        for line in lines:
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError:
                continue
            if not tokens:
                continue
            key, _, inline = tokens[0].partition("=")
            values = ([inline] if inline else []) + tokens[1:]
            if key.lower() == "host":
                found.update(value for value in values if value and not any(c in value for c in "*?![]"))
            elif key.lower() == "include":
                for value in values:
                    candidate = Path(value).expanduser()
                    if not candidate.is_absolute():
                        candidate = Path.home() / ".ssh" / candidate
                    for match in sorted(glob.glob(str(candidate))):
                        scan(match, depth + 1)

    scan(filename)
    return sorted(found)

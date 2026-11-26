import json
import os
import sys

import pytest

from log_search.config import host_aliases
from log_search.controller import Controller, request
from log_search.transport import SshTransport


async def test_ssh_source_frame_and_remote_execution(tmp_path, monkeypatch):
    """A fake SSH executable runs the actual remote bootstrap and worker pipes."""
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "ssh"
    capture = tmp_path / "ssh-args.jsonl"
    fake.write_text("#!" + sys.executable + "\n" + "import json,os,sys\n" + "with open(os.environ['SSH_TEST_ARGS'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n" + "if '-O' in sys.argv or '-M' in sys.argv: sys.exit(0)\n" + "os.execl('/bin/sh','sh','-c',sys.argv[-1])\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SSH_TEST_ARGS", str(capture))
    root = tmp_path / "remote root'with quote"
    logs = root / "app-alpha/logs/history"
    logs.mkdir(parents=True)
    (logs / "info.1").write_text("2026-01-01 10:00:00 INFO TRACE\n")
    config = tmp_path / "ssh config"
    config.write_text("Host example\n  RequestTTY force\n  RemoteCommand cd /unused && exec bash -l\n")
    controller = Controller(str(config))
    try:
        task = await controller.start(request(root=str(root), query="TRACE"), "example")
        events = [event async for event in task.events()]
        assert task.done["scope_complete"], events
        assert task.entries[0]["service"] == "app-alpha"
        assert {path.name for path in root.iterdir()} == {"app-alpha"}
    finally:
        await controller.close()
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    actual = next(call for call in calls if call[-1].startswith("python3 -u"))
    assert actual[actual.index("-F") + 1] == str(config)
    assert "-T" in actual and "RemoteCommand=none" in actual
    assert str(root) not in actual[-1] and "TRACE" not in actual[-1]


def test_ssh_includes_aliases_and_options(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "extra").write_text("Host included !excluded wild-*\n")
    config = ssh / "config"
    config.write_text('Include extra\nHost one two\nHost *\nHost=three\n')
    assert host_aliases(config) == ["included", "one", "three", "two"]


async def test_missing_ssh_reports_failure_without_hang(tmp_path):
    controller = Controller(str(tmp_path / "no-config"))
    try:
        task = await controller.start(request(root=str(tmp_path), query="TRACE", timeout=1), "example")
        events = [event async for event in task.events()]
        assert task.done["outcome"] == "failed"
        assert any(event["type"] == "warning" for event in events)
    finally:
        await controller.close()


async def test_display_target_uses_openssh_config_without_connection(tmp_path):
    included = tmp_path / "included"
    included.write_text("Host lab\n  HostName 192.0.2.218\n  User operator\n")
    config = tmp_path / "ssh_config"
    config.write_text('Include "{}"\nHost *\n  RequestTTY force\n  RemoteCommand exit 99\n'.format(included))
    transport = SshTransport(str(config))
    assert await transport.display_target("lab") == "operator@192.0.2.218"
    assert await transport.display_target("root@lab") == "root@192.0.2.218"
    assert transport.directory is None and not transport.masters
    broken = SshTransport(str(tmp_path / "missing"))
    with pytest.raises(ValueError):
        await broken.display_target("lab")

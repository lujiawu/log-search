import gzip
import json

import pytest
from textual.widgets import Input, SelectionList, Static, TextArea

from log_search import worker
from log_search.controller import request
from log_search.dialogs import DirectoryDialog, SettingsDialog
from log_search.tui import LogSearchApp


@pytest.fixture(autouse=True)
def default_log_day(monkeypatch):
    monkeypatch.setattr("log_search.tui.today_time_range", lambda: ("2026-01-01 00:00:00", "2026-01-02 00:00:00"))


def create_root(tmp_path):
    root = tmp_path / "deployment"
    for service in ("app-alpha", "app-beta"):
        (root / service / "logs" / "history").mkdir(parents=True)
    (root / "app-beta" / "logs" / "system.log").write_text("2026-01-01 10:00:01 ERROR TRACE\n")
    (root / "app-beta" / "logs" / "history" / "system.1").write_text("2026-01-01 11:00:00 INFO TRACE\n")
    (root / "app-alpha" / "logs" / "info").write_text("2026-01-01 12:00:00 INFO quiet\n")
    (root / "app-alpha" / "logs" / "info.gz").write_bytes(gzip.compress(b"2026-01-01 12:00:00 INFO gzip TRACE\n"))
    (root / "no-logs").mkdir()
    return root


def execute(value):
    events = []
    worker.execute(value, events.append)
    return events


def test_comma_separated_directories_round_trip():
    from log_search.dialogs import format_directories, parse_directory_values
    assert format_directories(["/data/x/app-beta/logs", "/data/x/nacos/logs"], "/data/x") == "app-beta/logs, nacos/logs"
    assert format_directories(["/other/logs", "/data/x/b/logs"], "/data/x") == "/other/logs, b/logs"
    assert format_directories([], "") == ""
    assert parse_directory_values(["app-beta/logs, nacos/logs", "/data/app logs"], "/data/x") == ["/data/x/app-beta/logs", "/data/x/nacos/logs", "/data/app logs"]
    assert parse_directory_values([""], "/data/x") == []
    assert parse_directory_values(["app-beta/logs"], "") == ["app-beta/logs"]


def test_logs_action_lists_services_without_bodies(tmp_path):
    root = create_root(tmp_path)
    events = execute(request(action="logs", root=str(root)))
    listing = next(event["directories"] for event in events if event["type"] == "logs")
    assert [item["service"] for item in listing] == ["app-alpha", "app-beta"]
    assert [item["files"] for item in listing] == [2, 2]
    assert [item["path"] for item in listing] == [str(root / "app-alpha" / "logs"), str(root / "app-beta" / "logs")]
    assert all(item["bytes"] > 0 for item in listing)
    assert "gzip TRACE" not in "".join(json.dumps(event, ensure_ascii=False) for event in events)
    done = events[-1]
    assert done["outcome"] == "completed" and done["scope_basis"] == "log_directory_listing"


def test_root_with_selected_directories_keeps_relative_display(tmp_path):
    root = create_root(tmp_path)
    events = execute(request(root=str(root), log_dirs=[str(root / "app-alpha" / "logs")], query="TRACE"))
    entries = [event["entry"] for event in events if event["type"] == "entry"]
    assert [(entry["file"], entry["service"]) for entry in entries] == [("app-alpha/logs/info.gz", "app-alpha")]
    assert events[-1]["scope_complete"]


def test_selected_directories_must_exist_under_root(tmp_path):
    root = create_root(tmp_path)
    with pytest.raises(ValueError):
        request(root=str(root), log_dirs=[str(tmp_path / "elsewhere")], query="TRACE")
    events = execute(request(root=str(root), log_dirs=[str(root / "no-logs" / "logs")], query="TRACE"))
    assert events[-1]["outcome"] == "failed"
    assert any("所选日志目录" in event.get("message", "") for event in events if event["type"] == "warning")


async def wait_screen(pilot, app, kind, timeout=200):
    for _ in range(timeout):
        await pilot.pause(.05)
        if isinstance(app.screen, kind):
            return app.screen
    raise AssertionError("{} did not open".format(kind.__name__))


async def open_picker(pilot, app):
    await pilot.click("#target")
    settings = await wait_screen(pilot, app, SettingsDialog)
    await pilot.click("#pick-dirs")
    return settings, await wait_screen(pilot, app, DirectoryDialog)


async def confirm_picker(pilot, app, paths):
    listing = app.screen.query_one("#directories", SelectionList)
    listing.deselect_all()
    for path in paths:
        listing.select(path)
    await pilot.press("ctrl+s")
    await pilot.pause()
    assert isinstance(app.screen, SettingsDialog)


async def wait_finished(pilot, app):
    for _ in range(100):
        await pilot.pause(.05)
        if app.search_task is not None and app.search_task.done is not None:
            await pilot.pause(.2)
            return
    raise AssertionError("search did not finish")


async def test_picker_in_settings_writes_draft_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = create_root(tmp_path)
    app = LogSearchApp(root=str(root))
    async with app.run_test(size=(90, 30)) as pilot:
        settings, dialog = await open_picker(pilot, app)
        assert [option.value for option in dialog.query_one("#directories", SelectionList).options] == [str(root / "app-alpha" / "logs"), str(root / "app-beta" / "logs")]
        await confirm_picker(pilot, app, [str(root / "app-beta" / "logs")])
        editor = app.screen.query_one("#editor", TextArea)
        assert "日志目录 = app-beta/logs" in editor.text
        assert "根目录 = " + str(root) in editor.text
        # Multiple picked directories render as one comma-separated relative line.
        await pilot.click("#pick-dirs")
        await wait_screen(pilot, app, DirectoryDialog)
        await confirm_picker(pilot, app, [str(root / "app-beta" / "logs"), str(root / "app-alpha" / "logs")])
        assert "日志目录 = app-beta/logs, app-alpha/logs" in app.screen.query_one("#editor", TextArea).text
        await pilot.click("#pick-dirs")
        await wait_screen(pilot, app, DirectoryDialog)
        await confirm_picker(pilot, app, [str(root / "app-beta" / "logs")])
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.settings["log_dirs"] == [str(root / "app-beta" / "logs")]
        assert "1目录" in str(app.query_one("#target").label)
        app.query_one("#query", Input).value = "TRACE"
        value = app.compose_request()
        assert value["scope"] == {"root": str(root), "log_dirs": [str(root / "app-beta" / "logs")]}
        await pilot.press("enter")
        await wait_finished(pilot, app)
        assert {entry["file"] for entry in app.search_task.entries} == {"app-beta/logs/system.log", "app-beta/logs/history/system.1"}
        assert app.search_task.done["scope_complete"]
        await app.action_quit()
    reopened = LogSearchApp(root=str(root))
    assert reopened.settings["log_dirs"] == [str(root / "app-beta" / "logs")]


async def test_picker_escape_keeps_scope_until_save(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = create_root(tmp_path)
    app = LogSearchApp(root=str(root))
    async with app.run_test(size=(90, 30)) as pilot:
        await open_picker(pilot, app)
        await confirm_picker(pilot, app, [str(root / "app-beta" / "logs")])
        # Escaping the whole settings dialog must discard the uncommitted pick.
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.settings["log_dirs"] == []
        await app.action_quit()
    reopened = LogSearchApp(root=str(root))
    assert reopened.settings["log_dirs"] == []


async def test_picker_requires_at_least_one_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = create_root(tmp_path)
    app = LogSearchApp(root=str(root))
    async with app.run_test(size=(90, 30)) as pilot:
        await open_picker(pilot, app)
        app.screen.query_one("#directories", SelectionList).deselect_all()
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, DirectoryDialog)
        assert "至少勾选" in str(app.screen.query_one("#error", Static).render())
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, SettingsDialog)
        await pilot.press("escape")
        await app.action_quit()


async def test_discovery_failure_shows_error_in_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = LogSearchApp(root=str(tmp_path / "missing"))
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.click("#target")
        await wait_screen(pilot, app, SettingsDialog)
        await pilot.click("#pick-dirs")
        for _ in range(200):
            await pilot.pause(.05)
            if "发现失败" in str(app.screen.query_one("#error", Static).render()):
                break
        assert "发现失败" in str(app.screen.query_one("#error", Static).render())
        assert isinstance(app.screen, SettingsDialog)
        assert app.settings["log_dirs"] == []
        await pilot.press("escape")
        await app.action_quit()


async def test_manual_directories_without_root_remain_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    logs = tmp_path / "downloaded archives" / "misc"
    logs.mkdir(parents=True)
    (logs / "app.log").write_text("2026-01-01 10:00:00 INFO TRACE\n")
    app = LogSearchApp(root=str(tmp_path))
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.click("#target")
        settings = await wait_screen(pilot, app, SettingsDialog)
        editor = settings.query_one("#editor", TextArea)
        editor.load_text("主机 = \n根目录 = \n日志目录 = {}\n开始时间 = \n结束时间 = \n包含gzip = 是\n包含未知时间 = 是".format(logs))
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.settings["mode"] == "dirs" and app.settings["log_dirs"] == [str(logs)]
        app.query_one("#query", Input).value = "TRACE"
        value = app.compose_request()
        assert value["scope"] == {"log_dirs": [str(logs)]}
        await pilot.press("enter")
        await wait_finished(pilot, app)
        assert [entry["file"] for entry in app.search_task.entries] == [str(logs / "app.log")]
        await app.action_quit()

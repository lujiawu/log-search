import asyncio

import pytest
from textual.document._document import Selection
from textual.widgets import Button, DataTable, Input, Static, TextArea

from log_search.dialogs import SettingsDialog
from log_search.transport import local_worker
from log_search.tui import DetailScreen, LogSearchApp, LogText


@pytest.fixture(autouse=True)
def default_log_day(monkeypatch):
    monkeypatch.setattr("log_search.tui.today_time_range", lambda: ("2026-01-01 00:00:00", "2026-01-02 00:00:00"))


async def wait_finished(pilot, app):
    for _ in range(100):
        await pilot.pause(.05)
        if app.search_task is not None and app.search_task.done is not None:
            await pilot.pause(.2)
            return
    raise AssertionError("search did not finish")


def create_logs(tmp_path):
    logs = tmp_path / "deployment" / "app-beta" / "logs"
    logs.mkdir(parents=True)
    (logs / "system.log").write_text("2026-01-01 10:00:00 INFO before\n2026-01-01 10:00:01 ERROR TRACE\n at demo\n2026-01-01 10:00:02 INFO TRACE\nAuthorization: Bearer private\n")
    return logs.parent.parent


async def test_minimal_search_detail_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Exercise deterministic OSC52 fallback without requiring a graphical session.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    app = LogSearchApp(root=str(create_logs(tmp_path)))
    async with app.run_test(size=(80, 24)) as pilot:
        table = app.query_one("#results", DataTable)
        assert table.size.height == 21
        assert app.query_one("#query-separator").region.y == 1
        assert table.region.y == 2
        assert not app.query("#operations")
        assert not table.show_header
        assert app.query_one("#query", Input).size.height == 1
        app.query_one("#query", Input).value = "TRACE"
        await pilot.press("enter")
        await wait_finished(pilot, app)
        assert table.row_count == 2 and app.search_task.done["scope_complete"]
        table.focus()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        detail = app.screen.query_one("#detail", LogText)
        assert "private" not in detail.text and "[REDACTED]" in detail.text
        assert detail.read_only
        # Selecting text then Ctrl+C must copy, not cancel search or exit the app.
        detail.selection = Selection((0, 0), (0, 10))
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.clipboard == detail.text[:10]
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("ctrl+shift+c")
        await pilot.pause()
        assert app.clipboard == detail.text[:10]
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert app.clipboard == detail.text
        await pilot.click("#copy")
        assert "OSC52" in str(app.screen.query_one("#detail-status", Static).render())
        await pilot.click("#context")
        for _ in range(50):
            await pilot.pause(.05)
            if "已加载" in str(app.screen.query_one("#detail-status", Static).render()):
                break
        assert "相邻条目" in app.screen.query_one("#detail", LogText).text
        await pilot.press("escape")
        assert len(app.screen_stack) == 1
        assert app.selected_id == app.visible_entries[1]["id"]
        await app.action_quit()
    assert (tmp_path / "config/log-search/config.json").exists()


async def test_single_click_only_selects_double_click_opens(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = LogSearchApp(root=str(create_logs(tmp_path)))
    async with app.run_test(size=(120, 36)) as pilot:
        app.query_one("#query", Input).value = "TRACE"
        await pilot.press("enter")
        await wait_finished(pilot, app)
        # Even clicking the already selected row once must not open details.
        await pilot.click("#results", offset=(4, 0))
        await pilot.pause()
        assert len(app.screen_stack) == 1
        await pilot.double_click("#results", offset=(4, 1))
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.entry["level"] == "INFO"
        await pilot.press("escape")
        assert app.query_one("#results", DataTable).cursor_row == 1
        await app.action_quit()


async def test_target_settings_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = str(create_logs(tmp_path))
    config = tmp_path / "ssh_config"
    config.write_text("Host test-server\n  HostName 127.0.0.1\n")
    app = LogSearchApp(root=root, ssh_config=str(config))
    async with app.run_test(size=(80, 24)) as pilot:
        await app.action_search()
        assert app.search_task is None
        assert "查询值必填" in str(app.query_one("#status", Static).render())
        await pilot.press("r", "x", "q", "/")
        assert app.query_one("#query", Input).value == "rxq/"
        await pilot.click("#target")
        await pilot.pause()
        assert isinstance(app.screen, SettingsDialog)
        editor = app.screen.query_one("#editor", TextArea)
        assert "根目录 = " + root in editor.text
        assert "开始时间 = 2026-01-01 00:00:00" in editor.text
        assert "结束时间 = 2026-01-02 00:00:00" in editor.text
        assert editor.has_focus
        draft = editor.text.replace("2026-01-01 00:00:00", "2026-01-01 10:00:02").replace("2026-01-02 00:00:00", "2026-01-01 10:00:00")
        editor.load_text(draft)
        await pilot.press("ctrl+s")
        assert "开始时间必须早于结束时间" in str(app.screen.query_one("#error", Static).render())
        assert editor.text == draft and app.settings["since"] == "2026-01-01 00:00:00"
        editor.load_text(draft.replace("10:00:02", "10:00:01").replace("10:00:00", "10:00:02"))
        # Save must be reachable with the mouse even when validation adds a message.
        await pilot.pause()
        await pilot.click("#apply")
        await pilot.pause()
        assert app.settings["since"] == "2026-01-01 10:00:01"
        assert app.query_one("#query", Input).has_focus
        app.query_one("#query", Input).value = "TRACE"
        await pilot.press("enter")
        await wait_finished(pilot, app)
        assert len(app.search_task.entries) == 1
        await app.action_quit()


async def test_quit_immediately_after_search(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    logs = tmp_path / "app-alpha/logs"
    logs.mkdir(parents=True)
    (logs / "rolling").write_text("2026-01-01 10:00:00 INFO TRACE\n" * 10000)
    app = LogSearchApp(root=str(tmp_path))
    async with app.run_test(size=(120, 36)):
        app.query_one("#query", Input).value = "TRACE"
        await app.action_search()
        await asyncio.wait_for(app.action_quit(), 5)
        assert app.search_task.runner.done()


async def test_partial_result_marker_survives_edits_and_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    app = LogSearchApp(root=str(create_logs(tmp_path)))
    original = app.compose_request

    def limited_request():
        value = original()
        value["limits"]["max_entries"] = 1
        return value

    monkeypatch.setattr(app, "compose_request", limited_request)
    async with app.run_test(size=(80, 24)) as pilot:
        app.query_one("#query", Input).value = "TRACE"
        await pilot.press("enter")
        await wait_finished(pilot, app)
        assert app.search_task.done["outcome"] == "limited"
        app.query_one("#query", Input).value = "next query"
        await pilot.pause()
        assert "部分结果" in str(app.query_one("#status", Static).render())
        app.query_one("#results", DataTable).focus()
        await pilot.press("enter", "ctrl+c", "escape")
        assert "部分结果" in str(app.query_one("#status", Static).render())
        assert app.clipboard
        await app.action_quit()


async def test_idle_ctrl_c_keeps_app_and_input_q_is_literal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = LogSearchApp(root=str(create_logs(tmp_path)))
    async with app.run_test(size=(80, 24)) as pilot:
        field = app.query_one("#query", Input)
        await pilot.press("q", "Q", "?", "e")
        assert field.value == "qQ?e"
        assert len(app.screen_stack) == 1 and not app.closing
        await pilot.press("ctrl+c")
        assert field.value == "qQ?e" and not app.closing
        app.query_one("#results", DataTable).focus()
        await pilot.press("ctrl+p")
        assert len(app.screen_stack) == 1
        await pilot.press("ctrl+c")
        assert not app.closing
        await pilot.press("?")
        assert len(app.screen_stack) == 1
        await pilot.press("e")
        assert len(app.screen_stack) == 1
        await pilot.press("q")
        assert app.closing


async def test_resolved_target_label_keeps_alias_and_ignores_old_result(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = tmp_path / "ssh_config"
    config.write_text("Host lab\n  User root\n  HostName 192.0.2.218\n")
    app = LogSearchApp(host="lab", root=str(tmp_path), ssh_config=str(config))
    async with app.run_test(size=(80, 24)) as pilot:
        for _ in range(30):
            await pilot.pause(.05)
            if app.target_label == "root@192.0.2.218":
                break
        target = app.query_one("#target", Button)
        assert str(target.label) == "root@192.0.2.218"
        assert target.region.width >= len("root@192.0.2.218") + 2
        assert app.host == "lab" and app.query_one("#query", Input).size.width >= 35
        await pilot.click("#target")
        assert "主机 = root@192.0.2.218" in app.screen.query_one("#editor", TextArea).text
        await pilot.press("escape")
        app.settings["host"] = None
        app.update_target()
        # A previous lookup must never replace the newly selected local target.
        await app.resolve_target("lab")
        await pilot.pause()
        assert str(target.label) == "本机"
        app.settings["host"] = "lab"
        app.update_target()
        await pilot.pause(.3)
        assert str(target.label) == "root@192.0.2.218"
        assert target.region.width >= len("root@192.0.2.218") + 2
        app.controller.ssh.config = str(tmp_path / "missing")
        app.settings["host"] = "fallback-alias"
        app.update_target()
        await pilot.pause(.2)
        assert str(target.label) == "fallback-alias"
        assert not app.closing
        await app.action_quit()


async def test_settings_endpoint_is_used_for_ssh_and_can_be_edited(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = tmp_path / "ssh_config"
    config.write_text("Host lab\n  User root\n  HostName 192.0.2.218\n")
    app = LogSearchApp(host="lab", root=str(create_logs(tmp_path)), ssh_config=str(config))
    connected_targets = []

    async def capture_open(target, *, task_id=None):
        connected_targets.append(target)
        return await local_worker()

    monkeypatch.setattr(app.controller.ssh, "open", capture_open)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#target")
        editor = app.screen.query_one("#editor", TextArea)
        assert editor.text.startswith("主机 = root@192.0.2.218\n")
        await pilot.press("escape")
        assert app.host == "lab"  # Opening alone must not change connection settings.
        await pilot.click("#target")
        await pilot.pause()
        await pilot.press("ctrl+s")
        assert app.host == "root@192.0.2.218"
        app.query_one("#query", Input).value = "TRACE"
        await pilot.press("enter")
        await wait_finished(pilot, app)
        assert connected_targets == ["root@192.0.2.218"]
        assert app.search_task.host == "root@192.0.2.218"
        assert len(app.search_task.entries) == 2
        await pilot.click("#target")
        editor = app.screen.query_one("#editor", TextArea)
        editor.load_text(editor.text.replace("root@192.0.2.218", "operator@192.0.2.219"))
        await pilot.press("ctrl+s", "enter")
        await wait_finished(pilot, app)
        assert app.host == "operator@192.0.2.219"
        assert connected_targets == ["root@192.0.2.218", "operator@192.0.2.219"]
        assert app.search_task.host == "operator@192.0.2.219"
        await pilot.click("#target")
        assert app.screen.query_one("#editor", TextArea).text.startswith("主机 = operator@192.0.2.219\n")
        await pilot.press("escape")
        await app.action_quit()


async def test_stats_table_and_full_stack_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    logs = tmp_path / "app-beta/logs"
    logs.mkdir(parents=True)
    frames = "".join(" at com.foo.Frame{}(Frame.java:{})\n".format(i, i) for i in range(220))
    (logs / "system.log").write_text(
        "2026-01-01 10:00:00 INFO before\n"
        "2026-01-01 10:00:01 ERROR java.lang.RuntimeException: boom\n" + frames +
        "2026-01-01 10:00:02 INFO after\n")
    app = LogSearchApp(root=str(tmp_path))
    async with app.run_test(size=(120, 36)) as pilot:
        table = app.query_one("#results", DataTable)
        table.focus()
        await pilot.press("e")
        await wait_finished(pilot, app)
        assert app.mode == "stats" and app.stats["exception_entries"] == 1
        for _ in range(20):
            await pilot.pause(.05)
            if table.row_count == 1:
                break
        assert table.show_header and table.row_count == 1
        assert str(table.get_row_at(0)[1]) == "java.lang.RuntimeException"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(.05)
            if isinstance(app.screen, DetailScreen):
                break
        assert isinstance(app.screen, DetailScreen)
        # 搜索预览截断在 150 行 / 16 KiB；统计详情必须展示完整堆栈
        assert "com.foo.Frame219" in app.screen.query_one("#detail", LogText).text
        await pilot.press("escape")
        await pilot.press("ctrl+f")
        app.query_one("#query", Input).value = "boom"
        await pilot.press("enter")
        await wait_finished(pilot, app)
        assert app.mode == "search" and not table.show_header
        await app.action_quit()


async def test_grouped_rows_details_retain_all_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = create_logs(tmp_path)
    logs = root / "app-beta/logs"
    (logs / "copy.log").write_bytes((logs / "system.log").read_bytes())
    app = LogSearchApp(root=str(root))
    async with app.run_test(size=(80, 24)) as pilot:
        app.query_one("#query", Input).value = "TRACE"
        await pilot.press("enter")
        await wait_finished(pilot, app)
        assert len(app.search_task.entries) == 4
        assert app.query_one("#results", DataTable).row_count == 2
        assert all(len(e["sources"]) == 2 for e in app.visible_entries)
        await pilot.press("enter")
        text = app.screen.query_one("#detail", LogText).text
        assert "2 个来源" in text and "copy.log" in text and "system.log" in text
        await app.action_quit()


async def test_ctrl_c_cancels_without_exit_then_q_cleans_running_task(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    opening = asyncio.Event()

    async def pending_worker():
        await opening.wait()
        raise AssertionError("worker start should have been cancelled")

    monkeypatch.setattr("log_search.controller.local_worker", pending_worker)
    app = LogSearchApp(root=str(create_logs(tmp_path)))
    async with app.run_test(size=(80, 24)) as pilot:
        app.query_one("#query", Input).value = "TRACE"
        await pilot.press("enter", "ctrl+c")
        await wait_finished(pilot, app)
        assert app.search_task.done["outcome"] == "cancelled"
        assert not app.closing
        await pilot.press("r")
        assert app.running
        task = app.search_task
        await pilot.press("q")
        assert app.closing and task.runner.done()
        assert task.done["outcome"] == "cancelled"

from datetime import datetime

import pytest
from textual.widgets import Button, Input, RadioSet, RadioButton, Static, TextArea

from log_search.controller import today_time_range
from log_search.dialogs import SettingsDialog, parse_fields
from log_search.tui import LogSearchApp


def pick_option(screen, key, option):
    """Click-free selection: flipping a RadioButton drives RadioSet like a real click would."""
    for button in screen.query_one("#setting-" + key, RadioSet).query(RadioButton):
        if str(button.label) == option:
            button.value = True


def test_today_range_handles_month_and_year_boundaries():
    assert today_time_range(datetime(2028, 2, 29, 15, 32)) == ("2028-02-29 00:00:00", "2028-03-01 00:00:00")
    assert today_time_range(datetime(2026, 12, 31, 23, 59)) == ("2026-12-31 00:00:00", "2027-01-01 00:00:00")


def test_editor_parser_preserves_path_spaces_and_equals():
    text = "# 可以直接修改\n主机 = demo开发环境\n目录 = /data/a b=c\n目录 = /data/二\n"
    assert parse_fields(text, ("主机", "目录"), repeated=("目录",)) == {"主机": "demo开发环境", "目录": ["/data/a b=c", "/data/二"]}
    for invalid in ("主机: local", "未知 = x", "主机 = a\n主机 = b", "主机 = local"):
        with pytest.raises(ValueError):
            parse_fields(invalid, ("主机", "目录"))


async def test_settings_dialog_buttons_keep_a_visible_label_row(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    app = LogSearchApp(root=str(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#target")
        assert isinstance(app.screen, SettingsDialog)
        buttons = list(app.screen.query(Button))
        # Textual 8 的默认按钮边框曾把 height:1 的按钮内容挤成 0 行，文字不可见。
        assert buttons and all(button.size.height >= 1 for button in buttons)
        await app.action_quit()


async def test_editor_keyboard_cancel_copy_multiple_directories_and_unlimited_time(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    app = LogSearchApp(root=str(tmp_path))
    initial = dict(app.settings)
    assert (initial["since"], initial["until"]) == today_time_range()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#target")
        assert isinstance(app.screen, SettingsDialog)
        editor = app.screen.query_one("#editor", TextArea)
        assert editor.has_focus
        await pilot.press("f7", "ctrl+c")
        assert app.clipboard == editor.text and isinstance(app.screen, SettingsDialog)
        await pilot.press("q", "?", "enter", "Q")
        assert editor.text == "q?\nQ" and not app.closing
        await pilot.press("escape")
        assert app.settings == initial
        await pilot.click("#target")
        editor = app.screen.query_one("#editor", TextArea)
        assert "开始时间 = " + initial["since"] in editor.text
        # Empty scope is rejected; root plus directories is now a valid subset scope.
        editor.load_text("主机 = \n根目录 = \n日志目录 = \n开始时间 = \n结束时间 = \n包含gzip = 是\n包含未知时间 = 否")
        await pilot.press("ctrl+s")
        assert "请填写根目录" in str(app.screen.query_one("#error", Static).render())
        assert app.settings == initial
        editor.load_text("主机 = \n根目录 = /tmp\n日志目录 = /tmp/logs\n开始时间 = \n结束时间 = \n包含gzip = 是\n包含未知时间 = 否")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.settings["mode"] == "root" and app.settings["log_dirs"] == ["/tmp/logs"]
        draft = "主机 = test-server\n根目录 = \n日志目录 = /data/app logs\n日志目录 = /data/another=one\n开始时间 = \n结束时间 = \n包含gzip = 否\n包含未知时间 = 是"
        await pilot.click("#target")
        editor = app.screen.query_one("#editor", TextArea)
        editor.load_text(draft)
        # The pasted 包含gzip line stays accepted but ignored; the radio row is authoritative.
        pick_option(app.screen, "archives", "否")
        await pilot.press("ctrl+s")
        assert len(app.screen_stack) == 1
        assert app.host == "test-server" and app.settings["mode"] == "dirs"
        assert app.settings["log_dirs"] == ["/data/app logs", "/data/another=one"]
        assert app.settings["since"] is None and app.settings["until"] is None
        app.query_one("#query", Input).value = "TRACE"
        value = app.compose_request()
        assert value["scope"] == {"log_dirs": app.settings["log_dirs"]}
        assert not value["include_archives"] and value["include_unknown_time"]
        # Clearing dates is deliberate; reopening must keep the cleared values.
        await pilot.click("#target")
        # Reopening shows the radio-backed options as pre-checked rows; text draft no longer carries them.
        reopened = app.screen.query_one("#editor", TextArea).text
        assert reopened.startswith("主机 = " + app.target_label + "\n根目录 = \n日志目录 = /data/app logs, /data/another=one\n开始时间 = \n结束时间 = \n包含未知时间 = 是\n")
        assert str(app.screen.query_one("#setting-archives", RadioSet).pressed_button.label) == "否"
        await pilot.press("escape")
        await app.action_quit()


async def test_radio_options_save_and_prefill_on_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = LogSearchApp(root=str(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#target")
        assert isinstance(app.screen, SettingsDialog)
        first = app.screen.query_one("#setting-mode", RadioSet).query(RadioButton)[0]
        assert first.region.width <= 12  # options hug their label; no 1fr stretching apart
        pick_option(app.screen, "mode", "完整条目")
        pick_option(app.screen, "expand", "否")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.settings["result_mode"] == "entries" and not app.settings["auto_expand_exceptions"]
        await pilot.click("#target")
        assert str(app.screen.query_one("#setting-mode", RadioSet).pressed_button.label) == "完整条目"
        assert str(app.screen.query_one("#setting-expand", RadioSet).pressed_button.label) == "否"
        await pilot.press("escape")
        await app.action_quit()

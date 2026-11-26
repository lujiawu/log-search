from textual.widgets import Input, RadioSet, Static, TextArea
from log_search.tui import LogSearchApp
from log_search.cli import parser


def test_cli_scan_options():
    args = parser().parse_args(["search", "--local", "--root", "/tmp", "--query", "TRACE"])
    assert args.concurrency == 2 and args.result_mode == "lines" and not args.all_file_dates
    args = parser().parse_args(["search", "--local", "--root", "/tmp", "--query", "TRACE", "--concurrency", "1", "--result-mode", "entries", "--all-file-dates", "--no-exception-expansion", "--expand-pattern", "RuntimeException"])
    assert args.concurrency == 1 and args.result_mode == "entries" and args.all_file_dates
    assert args.no_exception_expansion and args.expand_pattern == "RuntimeException"


def test_line_and_entry_groups_do_not_merge():
    from log_search.grouping import aggregate
    common = dict(service="demo", ts="2026-11-25 10:00:00", level="INFO", content_hash="same", raw_bytes=5, preview=["TRACE"], capture_complete=True, preview_truncated=False, source_changed=False)
    groups = aggregate([dict(common, id="a", file="a.log", content_kind="line"), dict(common, id="b", file="b.log", content_kind="entry")])
    assert len(groups) == 2


async def test_default_scan_settings_save_and_query(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    logs = tmp_path / "app-alpha/logs"
    logs.mkdir(parents=True)
    (logs / "log").write_text("2026-11-25 10:00:00 INFO TRACE\n continuation\n")
    app = LogSearchApp(root=str(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#target")
        area = app.screen.query_one("#editor", TextArea)
        assert "并发数 = 2" in area.text
        assert str(app.screen.query_one("#setting-mode", RadioSet).pressed_button.label) == "命中行"
        assert "按文件日期筛选 = 是" in area.text
        assert "返回条数 = 100" in area.text and "超时秒数 = 60" in area.text
        draft = area.text
        area.load_text(draft.replace("并发数 = 2", "并发数 = 4"))
        await pilot.press("ctrl+s")
        assert str(app.screen.query_one("#error", Static).render())
        area.load_text(draft.replace(app.settings["since"], "2026-11-25 00:00:00").replace(app.settings["until"], "2026-11-26 00:00:00").replace("返回条数 = 100", "返回条数 = 7").replace("超时秒数 = 60", "超时秒数 = 30"))
        await pilot.press("ctrl+s")
        assert app.settings["concurrency"] == 2 and app.settings["result_mode"] == "lines"
        app.query_one("#query", Input).value = "TRACE"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(.05)
            if app.search_task and app.search_task.done:
                break
        assert app.search_task.done["scope_complete"]
        assert app.search_task.request["limits"]["max_entries"] == 7 and app.search_task.request["limits"]["timeout_seconds"] == 30
        assert len(app.search_task.entries) == 1
        assert app.search_task.entries[0]["start_line"] == app.search_task.entries[0]["end_line"] == 1
        await app.action_quit()

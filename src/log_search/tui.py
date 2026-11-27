"""Compact query bar, separator, logs and status. Details open on demand."""

import asyncio
import re
from pathlib import Path

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Rule, Static, TextArea

from .clipboard import copy_text
from .config import load_preferences, save_preferences, ssh_config_path
from .controller import Controller, request, sort_key, today_time_range
from .grouping import aggregate
from .dialogs import DirectoryDialog, EditorDialog, SettingsDialog

SEARCH_COLUMNS = ("时间", "服务", "级别", "日志")
STATS_COLUMNS = ("次数", "异常类型", "关键帧", "首次", "最后", "样本")
SEARCH_HINT = "traceId / 文本 · Enter 查询"
STATS_HINT = "服务子目录（可空=全部）· Ctrl+D 选择 · Enter 统计"


class LogTable(DataTable):
    """A single click only selects, even when clicking the current row again."""

    async def _on_click(self, event: events.Click):
        event.prevent_default()
        row = event.style.meta.get("row")
        if row is None or not 0 <= row < self.row_count:
            return
        self.cursor_coordinate = Coordinate(row, 0)
        self._scroll_cursor_into_view(animate=False)
        if event.chain == 2:
            self._post_selected_message()
        event.stop()


class LogText(TextArea):
    def action_copy(self):
        self.screen.run_worker(self.screen.action_copy(), exclusive=True, group="clipboard")


class DetailScreen(ModalScreen):
    DEFAULT_CSS = """
    DetailScreen { background: $background; }
    DetailScreen #source { height: 1; padding: 0 1; text-overflow: ellipsis; color: $accent; }
    DetailScreen #detail { height: 1fr; border: none; padding: 0 1; }
    DetailScreen #detail-tools { height: 1; }
    DetailScreen Button { height: 1; min-width: 7; width: auto; border: none; padding: 0 1; }
    DetailScreen #detail-status { height: 1; width: 1fr; text-overflow: ellipsis; }
    """
    BINDINGS = [Binding("escape", "back", "返回"), Binding("ctrl+y", "copy_all", "复制全部", priority=True), Binding("ctrl+shift+c", "copy", "复制选择", priority=True), Binding("ctrl+o", "context", "上下文", priority=True)]

    def __init__(self, search_task, entry):
        super().__init__()
        self.search_task, self.entry = search_task, entry
        self.context_task = None

    def compose(self):
        suffix = " · 预览已截断" if self.entry["preview_truncated"] else ""
        kind = "命中行" if self.entry.get("content_kind") == "line" else "条目"
        yield Static("{}:{} · {} · {}{}".format(self.entry["file"], self.entry["start_line"], self.entry["service"], kind, suffix), id="source", markup=False)
        sources = self.entry.get("sources", [self.entry])
        source_lines = (["── 相同内容 · {} 个来源 ──".format(len(sources))] + ["{}:{}".format(e["file"], e["start_line"]) for e in sources] + [""]) if len(sources) > 1 else []
        yield LogText("\n".join(source_lines + self.entry["preview"]), read_only=True, soft_wrap=self.app.wrap, show_line_numbers=False, highlight_cursor_line=False, id="detail")
        with Horizontal(id="detail-tools"):
            yield Button("复制", id="copy", compact=True)
            yield Button("上下文", id="context", compact=True)
            yield Button("换行", id="wrap", compact=True)
            yield Button("返回", id="back", compact=True)
            yield Static("选中后 Ctrl+C 复制 · Ctrl+Y 全部 · Esc 返回", id="detail-status", markup=False)

    def on_mount(self):
        self.search_task.log("ui.detail_opened", entry_id=self.entry["id"], file=self.entry["file"], line=self.entry["start_line"], preview_truncated=self.entry["preview_truncated"])
        self.query_one("#source").tooltip = "{}:{}".format(self.entry["file"], self.entry["start_line"])
        self.query_one("#detail", LogText).focus()

    async def action_copy(self):
        area = self.query_one("#detail", LogText)
        self.search_task.log("ui.copy_requested", mode="selection" if area.selected_text else "detail", entry_id=self.entry["id"], characters=len(area.selected_text or area.text))
        message = await copy_text(self.app, area.selected_text or area.text)
        if self.is_mounted:
            self.query_one("#detail-status", Static).update(message)

    async def action_copy_all(self):
        self.search_task.log("ui.copy_requested", mode="detail_all", entry_id=self.entry["id"], characters=len(self.query_one("#detail", LogText).text))
        message = await copy_text(self.app, self.query_one("#detail", LogText).text)
        if self.is_mounted:
            self.query_one("#detail-status", Static).update(message)

    def action_context(self):
        if self.context_task and self.context_task.done is None:
            return
        self.run_worker(self.read_context(), exclusive=True, group="context")

    async def read_context(self):
        value = request("context", root=self.search_task.request["scope"].get("root"), log_dirs=self.search_task.request["scope"].get("log_dirs"), file=self.entry["file"], line=self.entry["start_line"], source_identity=self.entry["source_identity"], timeout=15)
        self.query_one("#detail-status", Static).update("读取上下文中…")
        task = await self.app.controller.start(value, self.search_task.host)
        self.context_task = task
        task.log("ui.context_started", parent_task_id=self.search_task.request["task_id"], entry_id=self.entry["id"])
        loaded = False
        try:
            async for event in task.events():
                if event["type"] == "context":
                    lines = []
                    for entry in event["entries"]:
                        label = "命中条目" if entry["start_line"] == self.entry["start_line"] else "相邻条目（可能含其他 trace）"
                        lines += ["── {} · {}:{} ──".format(label, entry["file"], entry["start_line"]), *entry["preview"]]
                    self.query_one("#detail", LogText).load_text("\n".join(lines))
                    self.query_one("#detail-status", Static).update("上下文已加载" + (" · 已截断 / 来源变化" if event.get("truncated") or event.get("source_changed") else ""))
                    loaded = True
                elif event["type"] == "warning":
                    self.query_one("#detail-status", Static).update(event["message"])
                elif event["type"] == "done" and not loaded:
                    self.query_one("#detail-status", Static).update("上下文未加载：" + event["outcome"])
        finally:
            await task.cancel()

    async def action_back(self):
        self.search_task.log("ui.detail_closed", entry_id=self.entry["id"])
        if self.context_task:
            await self.context_task.cancel()
        self.dismiss(None)

    async def on_unmount(self):
        if self.context_task:
            await self.context_task.cancel()

    @on(Button.Pressed)
    async def button(self, event):
        event.stop()
        self.search_task.log("ui.detail_button", button=event.button.id, entry_id=self.entry["id"])
        if event.button.id == "copy":
            await self.action_copy()
        elif event.button.id == "context":
            self.action_context()
        elif event.button.id == "wrap":
            area = self.query_one("#detail", LogText)
            area.soft_wrap = not area.soft_wrap
            self.app.wrap = area.soft_wrap
        elif event.button.id == "back":
            await self.action_back()


class LogSearchApp(App):
    TITLE = "log-search"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $background; }
    #query-line { height: 1; background: $surface; }
    #target { height: 1; width: auto; min-width: 5; max-width: 50%; border: none; padding: 0 1; }
    #query { height: 1; width: 1fr; border: none; padding: 0; }
    #tab-search, #tab-stats { height: 1; min-width: 9; width: auto; border: none; padding: 0 1; }
    #query-separator { height: 1; margin: 0; color: $text-muted; }
    #results { height: 1fr; border: none; padding: 0; }
    #status-line { height: 1; }
    #status { height: 1; width: 1fr; padding: 0 1; text-overflow: ellipsis; }
    #status.partial { color: $warning; }
    #key-hints { height: 1; width: auto; color: $text-muted; padding: 0 1; }
    """
    BINDINGS = [Binding("ctrl+c", "interrupt", "取消 / 复制", priority=True), Binding("ctrl+f", "query", "查询", priority=True), Binding("ctrl+d", "pick_dir", "选择目录", priority=True), Binding("q", "quit", "退出"), Binding("r", "search", "重查"), Binding("e", "stats", "异常统计"), Binding("x", "cancel", "取消")]

    def __init__(self, ssh_config=None, root=None, host=None):
        super().__init__()
        self.preferences = load_preferences()
        self.config_file = ssh_config_path(ssh_config)
        self.roots = self.preferences.get("roots", {})
        self.picked = self.preferences.get("picked", {})
        since, until = today_time_range()
        root = root or self.roots.get(host or "local", str(Path.cwd()))
        pick = self.picked.get(host or "local")
        log_dirs = list(pick["dirs"]) if pick and pick["root"] == root else []
        self.settings = dict(host=host, mode="root", root=root, log_dirs=log_dirs, since=since, until=until, include_archives=True, include_unknown_time=False, filter_file_dates=True, result_mode="lines", auto_expand_exceptions=True, expand_pattern="", concurrency=2, max_entries=100, timeout=60)
        self.wrap = bool(self.preferences.get("wrap", True))
        self.controller = Controller(self.config_file, role="tui")
        self.operations = self.controller.operations
        self.search_task = None
        self.visible_entries = []
        self.mode = "search"
        self.columns_mode = "search"
        self.query_text = ""
        self.stats_dir = ""
        self.stats = None
        self.stats_groups = []
        self.selected_id = None
        self.dirty_results = False
        self.redrawing = False
        self.reader = None
        self.last_stats = {}
        self.status_text = "输入查询后 Enter · 默认当天"
        self.closing = False
        self.target_label = host or "本机"

    @property
    def host(self):
        return self.settings["host"]

    def record(self, event, **fields):
        if self.search_task:
            fields.setdefault("task_id", self.search_task.request["task_id"])
            fields.setdefault("request_id", self.search_task.request["request_id"])
        self.operations.emit(event, **fields)

    def _handle_exception(self, error):
        self.operations.exception("ui.unhandled_exception", error, task_id=self.search_task.request["task_id"] if self.search_task else None)
        super()._handle_exception(error)

    @property
    def running(self):
        return self.search_task is not None and self.search_task.done is None

    def compose(self) -> ComposeResult:
        with Horizontal(id="query-line"):
            yield Button(self.host or "本机", id="target", compact=True)
            yield Input(placeholder=SEARCH_HINT, id="query", compact=True)
            yield Button("日志搜索", id="tab-search", compact=True, variant="primary")
            yield Button("堆栈统计", id="tab-stats", compact=True)
        yield Rule(id="query-separator")
        yield LogTable(id="results", cursor_type="row", show_header=False, show_row_labels=False, zebra_stripes=False)
        with Horizontal(id="status-line"):
            yield Static(self.status_text, id="status", markup=False)
            yield Static("e 统计 · q 退出", id="key-hints", markup=False)

    def on_mount(self):
        self.query_one("#results", DataTable).add_columns(*SEARCH_COLUMNS)
        self.record("ui.started", settings=self.settings, ssh_config=self.config_file, width=self.size.width, height=self.size.height)
        self.set_interval(.15, self.flush_results)
        theme = self.preferences.get("theme")
        if isinstance(theme, str) and theme in self.available_themes:
            self.theme = theme
        keys = self.preferences.get("keys", {})
        if isinstance(keys, dict):
            used = {"ctrl+c", "ctrl+p", "ctrl+f", "enter", "escape", "tab"}
            for action, key in keys.items():
                if action in ("search", "cancel", "quit", "query", "settings") and isinstance(key, str) and key and key not in used:
                    self.bind(key, action, description=action)
                    used.add(key)
        self.query_one("#query", Input).focus()
        self.update_target()
        if self.operations.error:
            self.set_status("操作日志不可用：" + self.operations.error)

    def scope_suffix(self):
        if self.settings["mode"] == "root" and self.settings["log_dirs"]:
            return " · {}目录".format(len(self.settings["log_dirs"]))
        return ""

    def update_target(self):
        target = self.query_one("#target", Button)
        self.target_label = self.host or "本机"
        target.label = Text(self.target_label + self.scope_suffix())
        target.refresh(layout=True)
        target.tooltip = "点击编辑查询设置 · {} · {}{}".format(self.host or "本机", self.settings["root"] if self.settings["mode"] == "root" else "；".join(self.settings["log_dirs"]), self.scope_suffix())
        options = []
        if self.settings["log_dirs"]:
            options.append("已勾选 {} 个日志目录".format(len(self.settings["log_dirs"])))
        if self.settings["since"] or self.settings["until"]:
            options.append("时间范围")
        if not self.settings["include_archives"]:
            options.append("无 gzip")
        if self.settings["since"] or self.settings["until"]:
            options.append("{} → {}".format(self.settings["since"] or "不限", self.settings["until"] or "不限"))
        self.query_one("#query", Input).tooltip = "字面量查询" + (" · " + " / ".join(options) if options else "")
        self.run_worker(self.resolve_target(self.host), exclusive=True, group="target-display")

    async def resolve_target(self, host):
        if not host:
            return
        try:
            label = await self.controller.ssh.display_target(host)
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            self.record("ui.target_display_failed", target=host, error_type=type(exc).__name__)
            return
        if not self.closing and self.host == host:
            self.target_label = label
            target = self.query_one("#target", Button)
            target.label = Text(label + self.scope_suffix())
            target.refresh(layout=True)
            self.record("ui.target_display_resolved", target=host, display=label)

    def on_resize(self, event):
        if hasattr(self, "operations"):
            self.record("ui.resized", width=event.size.width, height=event.size.height)

    def set_status(self, text=None):
        if text is not None:
            self.status_text = text
        dates = {entry["ts"].split(" ")[0] for entry in self.visible_entries if entry.get("ts")}
        suffix = ""
        if len(dates) == 1:
            suffix += " · " + next(iter(dates))
        done = self.search_task.done if self.search_task else None
        markers = []
        if done and not done.get("scope_complete") and "部分结果" not in self.status_text:
            markers.append("部分结果")
        if done and not done.get("content_complete", True) and "截断" not in self.status_text:
            markers.append("预览截断")
        prefix = " · ".join(markers) + " · " if markers else ""
        self.query_one("#status", Static).update(prefix + self.status_text + suffix)

    def compose_request(self):
        values = self.settings
        return request(host=self.host, root=values["root"] if values["mode"] == "root" else None, log_dirs=values["log_dirs"] or None, query=self.query_one("#query", Input).value, since=values["since"], until=values["until"], include_archives=values["include_archives"], include_unknown_time=values["include_unknown_time"], **{key: values[key] for key in ("timeout", "max_entries", "filter_file_dates", "result_mode", "auto_expand_exceptions", "expand_pattern", "concurrency") if key in values})

    def set_mode(self, mode):
        """Tab switch: swap the input value, placeholder and table columns; never starts a query."""
        if self.mode == mode or self.running:
            return
        field = self.query_one("#query", Input)
        if self.mode == "stats":
            self.stats_dir = field.value
        else:
            self.query_text = field.value
        self.mode = mode
        field.value = self.stats_dir if mode == "stats" else self.query_text
        field.placeholder = STATS_HINT if mode == "stats" else SEARCH_HINT
        self.query_one("#tab-search", Button).variant = "primary" if mode == "search" else "default"
        self.query_one("#tab-stats", Button).variant = "primary" if mode == "stats" else "default"
        self.dirty_results = True
        field.focus()

    async def action_stats(self):
        if len(self.screen_stack) != 1 or self.running:
            self.record("ui.stats_ignored", reason="task_running" if self.running else "dialog_open")
            return
        values = self.settings
        self.set_mode("stats")
        field = self.query_one("#query", Input)
        directory = field.value.strip()
        if directory and values["mode"] == "root" and not directory.startswith("/"):
            directory = str(Path(values["root"]) / directory)
        if directory:
            # 统计页的目录选择就是共享 scope：切回搜索页也是这个范围。
            field.value = self.stats_dir = directory
            values["log_dirs"] = [directory]
        self.record("ui.stats_submitted", query=directory, settings=values)
        try:
            value = request("exception-stats", root=values["root"] if values["mode"] == "root" else None,
                            log_dirs=values["log_dirs"] or None, since=values["since"], until=values["until"],
                            include_archives=values["include_archives"], include_unknown_time=values["include_unknown_time"],
                            timeout=600, top=10)
            self.search_task = await self.controller.start(value, self.host)
        except ValueError as exc:
            self.operations.exception("ui.stats_validation_failed", exc, settings=values)
            self.set_status(str(exc))
            return
        self.stats = None
        self.stats_groups = []
        self.selected_id = None
        self.visible_entries = []
        self.last_stats = {}
        self.query_one("#query", Input).disabled = True
        self.query_one("#target", Button).disabled = True
        self.query_one("#status").remove_class("partial")
        self.set_status("统计中… · Ctrl+C 取消")
        self.dirty_results = True
        self.reader = asyncio.create_task(self.consume(self.search_task))
        self.reader.add_done_callback(self.reader_finished)
        self.record("ui.stats_started", request=value)
        self.query_one("#results", DataTable).focus()

    async def action_search(self):
        if len(self.screen_stack) != 1 or self.running:
            self.record("ui.search_ignored", reason="task_running" if self.running else "dialog_open")
            return
        self.set_mode("search")
        self.record("ui.search_submitted", task_id=None, request_id=None, previous_task_id=self.search_task.request["task_id"] if self.search_task else None, settings=self.settings, query=self.query_one("#query", Input).value)
        self.dirty_results = True
        try:
            value = self.compose_request()
            self.search_task = await self.controller.start(value, self.host)
            self.query_one("#query", Input).value = value["query"]
        except ValueError as exc:
            self.operations.exception("ui.search_validation_failed", exc, settings=self.settings, query=self.query_one("#query", Input).value)
            self.set_status(str(exc))
            return
        self.selected_id = None
        self.visible_entries = []
        self.last_stats = {}
        self.query_one("#query", Input).disabled = True
        self.query_one("#target", Button).disabled = True
        self.query_one("#status").remove_class("partial")
        self.set_status("连接 / 检查中… · Ctrl+C 取消")
        self.dirty_results = True
        self.reader = asyncio.create_task(self.consume(self.search_task))
        self.reader.add_done_callback(self.reader_finished)
        self.record("ui.search_started", request=value)
        self.query_one("#results", DataTable).focus()
        if value["scope"].get("root"):
            self.roots[self.host or "local"] = value["scope"]["root"]

    def reader_finished(self, future):
        if future.cancelled():
            self.record("ui.event_consumer_cancelled", level="warning")
            return
        error = future.exception()
        if error is not None:
            self.operations.exception("ui.event_consumer_failed", error, task_id=self.search_task.request["task_id"] if self.search_task else None)
            if not self.closing:
                self.set_status("界面事件处理失败；运行 log-search diagnostics 查看操作日志")
                if self.search_task:
                    asyncio.create_task(self.search_task.cancel())

    async def consume(self, task):
        async for event in task.events():
            if self.closing:
                continue
            if event["type"] == "progress":
                self.last_stats = event
                if self.mode == "stats":
                    self.set_status("统计中 {}s · {} 文件 · {} MiB · Ctrl+C 取消".format(event.get("elapsed", "?"), event.get("scanned", 0), round(event.get("bytes_read", 0) / 1048576, 1)))
                else:
                    self.set_status("搜索中 {}s · {} 文件 · {}条 · Ctrl+C 取消".format(event["elapsed"], event["scanned"], event["entries"]))
            elif event["type"] == "entry":
                self.dirty_results = True
            elif event["type"] == "exception_stats":
                self.stats = event
                self.dirty_results = True
            elif event["type"] == "warning":
                self.set_status(event.get("message", ""))
            elif event["type"] == "done":
                self.last_stats = event
                labels = dict(completed="完成", cancelled="已取消", timed_out="已超时", limited="达到上限", failed="失败")
                if self.mode == "stats":
                    stats = self.stats or {}
                    text = "{} {}s · 总条目 {} · 异常 {} · 率 {:.2%} · {} 组".format(labels.get(event.get("outcome"), event.get("outcome")), event.get("elapsed", "?"), stats.get("total_entries", 0), stats.get("exception_entries", 0), stats.get("exception_rate", 0), stats.get("groups_total", 0))
                else:
                    text = "{} {}s · {}条 · {}".format(labels.get(event["outcome"], event["outcome"]), event.get("elapsed", "?"), len(task.entries), "范围完整" if event["scope_complete"] else "部分结果")
                if not event.get("content_complete", True):
                    text += " · 预览截断"
                if event.get("time_unknown"):
                    text += " · 时间未知 {}".format(event["time_unknown"])
                if event.get("filter_file_dates"):
                    text += " · 文件日期筛选 / 跳过{}".format(event.get("skipped_time_files", 0))
                self.set_status(text)
                self.query_one("#status").set_class(not event["scope_complete"], "partial")
                self.query_one("#query", Input).disabled = False
                self.query_one("#target", Button).disabled = False
                self.dirty_results = True

    def flush_results(self):
        if not self.dirty_results or len(self.screen_stack) != 1:
            return
        self.dirty_results = False
        table = self.query_one("#results", DataTable)
        if self.columns_mode != self.mode:
            table.clear(columns=True)
            table.add_columns(*(STATS_COLUMNS if self.mode == "stats" else SEARCH_COLUMNS))
            table.show_header = self.mode == "stats"
            self.columns_mode = self.mode
        if self.mode == "stats":
            self.flush_stats(table)
            return
        entries = self.search_task.entries if self.search_task and self.search_task.request.get("action") == "search" else []
        filtered = sorted(entries, key=sort_key)
        self.visible_entries = aggregate(filtered)
        dates = {entry["ts"].split(" ")[0] for entry in self.visible_entries if entry.get("ts")}
        old_scroll = table.scroll_y
        self.redrawing = True
        table.clear()
        selected_row = 0
        for index, entry in enumerate(self.visible_entries):
            # Keep the date for cross-day evidence; remove duplicated log prefixes.
            stamp = (entry.get("ts") or "时间未知").rstrip("0").rstrip(".") if entry.get("ts") and "." in entry["ts"] else entry.get("ts") or "时间未知"
            if len(dates) == 1 and entry.get("ts"):
                stamp = stamp.split(" ", 1)[-1]
            summary = entry["preview"][0] if entry["preview"] else "[空预览]"
            summary = re.sub(r"^\s*(?:\[[^\]]+\])?\s*\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?\s*", "", summary)
            summary = re.sub(r"^" + re.escape(entry["level"]) + r"\b\s*", "", summary)
            if len(entry["sources"]) > 1:
                summary = "×{} 来源 · ".format(len(entry["sources"])) + summary
            table.add_row(Text(stamp), Text(entry["service"]), Text(entry["level"], style="red" if entry["level"] in ("ERROR", "FATAL") else ""), Text(summary), key=entry["id"])
            if entry["id"] == self.selected_id:
                selected_row = index
        if self.visible_entries:
            table.move_cursor(row=selected_row, scroll=False)
            self.selected_id = self.visible_entries[selected_row]["id"]
            table.scroll_to(y=old_scroll, animate=False, immediate=True)
        else:
            self.selected_id = None
        self.redrawing = False
        if len(filtered) != len(self.visible_entries):
            self.status_text = self.status_text.split(" · 聚合显示")[0] + " · 聚合显示 {} 行 / {} 条".format(len(self.visible_entries), len(filtered))
        else:
            self.status_text = self.status_text.split(" · 聚合显示")[0]
        self.set_status()
        self.record("ui.results_rendered", received_entries=len(entries), visible_entries=len(self.visible_entries), table_rows=table.row_count, selected_entry_id=self.selected_id, task_outcome=self.search_task.done.get("outcome") if self.search_task and self.search_task.done else "running")

    def flush_stats(self, table):
        self.redrawing = True
        table.clear()
        self.stats_groups = (self.stats or {}).get("groups", [])
        for index, group in enumerate(self.stats_groups):
            sample = group.get("sample") or {}
            table.add_row(
                Text("{:,}".format(group["count"])),
                Text(group["exception_type"]),
                Text(" ".join(group["key_frames"]) or "—"),
                Text((group.get("first_seen") or "—")[:19]),
                Text((group.get("last_seen") or "—")[:19]),
                Text("{}:{}".format(sample.get("file", ""), sample.get("start_line", ""))),
                key="g{}".format(index))
        if self.stats_groups:
            table.move_cursor(row=0, scroll=False)
        self.redrawing = False
        self.set_status()
        self.record("ui.stats_rendered", groups=len(self.stats_groups), table_rows=table.row_count)

    def selected_entry(self):
        return next((entry for entry in self.visible_entries if entry["id"] == self.selected_id), None)

    @on(DataTable.RowHighlighted, "#results")
    def highlighted(self, event):
        if not self.redrawing:
            self.selected_id = event.row_key.value
            self.record("ui.entry_selected", entry_id=self.selected_id)

    @on(DataTable.RowSelected, "#results")
    def open_detail(self, event):
        if self.mode == "stats":
            self.run_worker(self.open_group(event.row_key.value), exclusive=True, group="group-detail")
            return
        entry = next((entry for entry in self.visible_entries if entry["id"] == event.row_key.value), None)
        if entry and self.search_task:
            self.selected_id = entry["id"]
            self.push_screen(DetailScreen(self.search_task, entry))

    async def open_group(self, key):
        """Fetch the sample entry once so the detail screen shows the whole stack."""
        try:
            group = self.stats_groups[int(key[1:])]
        except (ValueError, IndexError, AttributeError):
            return
        sample = group.get("sample") or {}
        if not sample.get("file") or self.search_task is None:
            return
        scope = self.search_task.request["scope"]
        value = request("context", root=scope.get("root"), log_dirs=scope.get("log_dirs"),
                        file=sample["file"], line=sample["start_line"], timeout=15)
        self.set_status("读取堆栈中…")
        try:
            task = await self.app.controller.start(value, self.search_task.host)
        except ValueError as exc:
            self.set_status(str(exc))
            return
        try:
            async for event in task.events():
                if event["type"] == "context":
                    entries = event.get("entries") or []
                    target = event.get("target_line")
                    entry = next((item for item in entries if target is not None and item["start_line"] <= target <= item["end_line"]), entries[0] if entries else None)
                    if entry:
                        self.push_screen(DetailScreen(self.search_task, entry))
                    return
                if event["type"] == "warning":
                    self.set_status(event.get("message", ""))
                elif event["type"] == "done":
                    self.set_status("堆栈未加载：" + str(event.get("outcome")))
        finally:
            await task.cancel()

    @on(Input.Submitted, "#query")
    async def submitted(self):
        if self.mode == "stats":
            await self.action_stats()
        else:
            await self.action_search()

    @on(Input.Changed, "#query")
    def query_changed(self):
        if self.query_one("#query", Input).value in (self.query_text, self.stats_dir):
            return
        if self.search_task and self.search_task.done:
            self.set_status("查询已更改 · 日志仍属上次查询")

    def action_query(self):
        if len(self.screen_stack) == 1 and not self.running:
            self.set_mode("search")
            self.query_one("#query", Input).focus()

    def action_pick_dir(self):
        if self.mode != "stats" or self.running or len(self.screen_stack) != 1:
            return
        self.run_worker(self.pick_dir(), exclusive=True, group="dir-pick")

    async def pick_dir(self):
        root = self.settings["root"] if self.settings["mode"] == "root" else None
        self.set_status("正在发现日志目录…")
        try:
            directories, _, _ = await self.controller.logs(request("logs", root=root, timeout=20), self.host)
        except (OSError, ValueError) as exc:
            self.operations.exception("ui.directory_discovery_failed", exc, target=self.host or "local", root=root)
            self.set_status(str(exc))
            return
        if not directories:
            self.set_status("未发现服务日志目录")
            return
        selected = await self.push_screen_wait(DirectoryDialog(directories, self.settings["log_dirs"]))
        if selected:
            # 统计只服务单个目录；同时写回共享 scope，搜索页跟随。
            self.settings["log_dirs"] = [selected[0]]
            self.stats_dir = selected[0]
            self.query_one("#query", Input).value = selected[0]
            self.set_status("目录已选择 · Enter 统计")

    async def action_settings(self):
        if len(self.screen_stack) != 1 or self.running:
            return
        host = self.host
        if host and self.target_label == host:
            await self.resolve_target(host)
        if self.closing or self.running or len(self.screen_stack) != 1 or self.host != host:
            return
        # Edit the actual endpoint; save commits the user@address target while
        # cancel keeps the original SSH alias and its configuration untouched.
        values = dict(self.settings, host=self.target_label if host else None)
        self.push_screen(SettingsDialog(self.config_file, values), self.apply_settings)

    def apply_settings(self, values):
        if values is None:
            return
        self.record("ui.settings_applied", previous_settings=self.settings, settings=values)
        self.commit_settings(values)

    def commit_settings(self, values):
        self.settings = values
        if values["mode"] == "root" and values.get("root"):
            self.picked[values["host"] or "local"] = {"root": values["root"], "dirs": list(values["log_dirs"])}
        self.update_target()
        self.set_status("查询选项已更改 · Enter 查询" + (" · 日志仍属上次查询" if self.search_task else ""))
        self.query_one("#query", Input).focus()

    async def action_cancel(self):
        if self.running:
            await self.search_task.cancel()

    async def action_interrupt(self):
        self.record("ui.interrupt", screen=type(self.screen).__name__, running=self.running)
        if isinstance(self.screen, DetailScreen):
            await self.screen.action_copy()
        elif isinstance(self.screen, EditorDialog):
            area = self.screen.query_one("#editor", TextArea)
            if area.selected_text:
                await copy_text(self, area.selected_text)
        elif len(self.screen_stack) > 1:
            self.screen.dismiss(None)
        elif self.running:
            await self.action_cancel()
        else:
            self.set_status()

    def on_key(self, event):
        if len(self.screen_stack) == 1 and isinstance(self.focused, DataTable) and event.key in ("j", "k"):
            (self.focused.action_cursor_down if event.key == "j" else self.focused.action_cursor_up)()
            event.stop()

    @on(Button.Pressed)
    async def button(self, event):
        self.record("ui.button", button=event.button.id)
        if event.button.id == "target":
            await self.action_settings()
        elif event.button.id == "tab-search":
            self.set_mode("search")
        elif event.button.id == "tab-stats":
            self.set_mode("stats")

    async def action_quit(self):
        self.record("ui.quit", running=self.running)
        self.closing = True
        if isinstance(self.screen, DetailScreen) and self.screen.context_task:
            await self.screen.context_task.cancel()
        try:
            save_preferences(dict(self.preferences, ssh_config=self.config_file, roots=self.roots, picked=self.picked, theme=self.theme, wrap=self.wrap))
            self.record("ui.preferences_saved")
        except OSError as exc:
            self.operations.exception("ui.preferences_save_failed", exc)
        await self.controller.close()
        if self.reader:
            await self.reader
        self.exit()

    async def on_unmount(self):
        self.closing = True
        await self.controller.close()

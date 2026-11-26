"""Plain text editors for infrequent settings; keep the workbench for logs."""

import asyncio
from pathlib import Path

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RadioButton, RadioSet, SelectionList, Static, TextArea

from .controller import request


def parse_fields(text, keys, repeated=(), optional=()):
    """No quoting or shell expansion; split only at the first equals sign."""
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in keys:
            raise ValueError("第 {} 行：请使用模板中的字段名和 =".format(number))
        if key in repeated:
            values.setdefault(key, []).append(value)
        elif key in values:
            raise ValueError("字段重复：" + key)
        else:
            values[key] = value
    missing = [key for key in keys if key not in values and key not in optional]
    if missing:
        raise ValueError("缺少字段：" + "、".join(missing))
    return values


def boolean(value, name):
    if value.lower() in ("是", "true", "yes", "1"):
        return True
    if value.lower() in ("否", "false", "no", "0"):
        return False
    raise ValueError(name + "请填写 是 或 否")


class Dialog(ModalScreen):
    DEFAULT_CSS = """
    Dialog { align: center middle; }
    Dialog > Vertical { width: 76; max-width: 100%; height: auto; max-height: 95%; background: $surface; border: round $accent; padding: 1; }
    Dialog Horizontal { height: 1; align-horizontal: right; }
    /* Textual 8 默认按钮带上下边框且优先级更高，height:1 时文字会被挤没 */
    Dialog Button { height: 1; min-width: 8; border: none !important; padding: 0 1; }
    Dialog #error { height: auto; max-height: 3; color: $warning; }
    """
    BINDINGS = [("escape", "dismiss(None)", "返回")]

    @on(Button.Pressed, "#back")
    def back(self):
        self.dismiss(None)


class EditorDialog(Dialog):
    failure_event = "ui.editor_save_failed"
    EXTRA_BUTTONS = ()
    RESERVED_LINES = 13
    DEFAULT_CSS = """
    EditorDialog #editor { border: none; padding: 0; }
    EditorDialog #editor-hint { height: auto; color: $text-muted; }
    """
    BINDINGS = [Binding("ctrl+s", "save", "保存", priority=True)]

    def __init__(self, title, text, hint):
        super().__init__()
        self.title_text, self.initial_text, self.hint = title, text, hint

    def compose(self):
        with Vertical():
            yield Label(self.title_text)
            yield from self.field_widgets()
            yield TextArea(self.initial_text, id="editor", soft_wrap=True, tab_behavior="focus")
            yield Static(self.hint + " · Ctrl+S 保存 · Esc 返回", id="editor-hint", markup=False)
            yield Static("", id="error", markup=False)
            with Horizontal():
                for label, button_id in self.EXTRA_BUTTONS:
                    yield Button(label, id=button_id)
                yield Button("返回", id="back")
                yield Button("保存", id="apply", variant="primary")

    def field_widgets(self):
        return ()

    def on_mount(self):
        self.resize_editor()
        self.query_one("#editor", TextArea).focus()

    def on_resize(self):
        if self.is_mounted:
            self.resize_editor()

    def resize_editor(self):
        # Reserve room for wrapped hints, errors and controls on small terminals.
        self.query_one("#editor", TextArea).styles.height = min(max(4, len(self.initial_text.splitlines()) + 1), max(3, self.app.size.height - self.RESERVED_LINES))

    @on(Button.Pressed, "#apply")
    def action_save(self):
        try:
            self.save_text(self.query_one("#editor", TextArea).text)
        except (ValueError, OSError) as exc:
            task = self.app.search_task
            self.app.operations.exception(self.failure_event, exc, dialog=type(self).__name__, task_id=task.request["task_id"] if task else None)
            self.query_one("#error", Static).update(str(exc))

    def save_text(self, text):
        raise NotImplementedError


def format_directories(paths, root):
    """One comma-separated line; directories under the root show as relative sub-directories."""
    shown = []
    base = Path(root) if root else None
    for path in paths:
        if base is not None:
            try:
                relative = Path(path).relative_to(base)
            except ValueError:
                relative = None
            if relative is not None and relative.parts:
                shown.append(str(relative))
                continue
        shown.append(path)
    return ", ".join(shown)


def parse_directory_values(values, root):
    # shortcut: a log path containing a comma would be mis-split; revisit if a real deployment has one.
    result = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.startswith("/") and root:
                part = str(Path(root) / part)
            result.append(part)
    return list(dict.fromkeys(result))


def replace_directories(text, root, paths):
    """Rewrite the 日志目录 block as one line, anchored below the 根目录 line."""
    kept = [line for line in text.splitlines() if line.partition("=")[0].strip() != "日志目录"]
    anchor = next((index for index, line in enumerate(kept) if line.partition("=")[0].strip() == "根目录"), 0)
    block = ["日志目录 = " + format_directories(paths, root)]
    return "\n".join(kept[:anchor + 1] + block + kept[anchor + 1:])


class SettingsDialog(EditorDialog):
    failure_event = "ui.settings_validation_failed"
    OPTIONS = ("按文件日期筛选", "内容模式", "异常自动展开", "展开正则", "并发数", "包含gzip", "返回条数", "超时秒数")
    KEYS = ("主机", "根目录", "日志目录", "开始时间", "结束时间", "包含未知时间") + OPTIONS
    EXTRA_BUTTONS = (("选择目录", "pick-dirs"),)
    BINDINGS = [Binding("ctrl+d", "pick_directories", "选择目录", priority=True)]
    RESERVED_LINES = 16  # editor is shorter: three radio rows sit above it
    DEFAULT_CSS = """
    SettingsDialog Horizontal.settings-row { height: 1; align-horizontal: left; }
    SettingsDialog .settings-label { width: 11; height: 1; }
    SettingsDialog RadioSet { layout: horizontal; height: 1; width: auto; }
    SettingsDialog RadioSet > RadioButton { width: auto; margin-right: 2; }
    """
    RADIO_FIELDS = (("内容模式", "mode", ("命中行", "完整条目")), ("包含gzip", "archives", ("是", "否")), ("异常自动展开", "expand", ("是", "否")))

    def __init__(self, config, settings):
        self.config, self.settings = config, settings
        self.radio_defaults = dict(mode="完整条目" if settings.get("result_mode") == "entries" else "命中行", archives="是" if settings["include_archives"] else "否", expand="是" if settings.get("auto_expand_exceptions", True) else "否")
        lines = ["主机 = " + (settings["host"] or ""), "根目录 = " + (settings["root"] if settings["mode"] == "root" else "")]
        lines += ["日志目录 = " + format_directories(settings["log_dirs"], settings["root"] if settings["mode"] == "root" else "")]
        lines += ["开始时间 = " + (settings["since"] or ""), "结束时间 = " + (settings["until"] or ""), "包含未知时间 = " + ("是" if settings["include_unknown_time"] else "否"), "返回条数 = " + str(settings.get("max_entries", 100)), "超时秒数 = " + str(settings.get("timeout", 60))]
        lines += ["按文件日期筛选 = " + ("是" if settings.get("filter_file_dates", True) else "否"), "展开正则 = " + settings.get("expand_pattern", ""), "并发数 = " + str(settings.get("concurrency", 2))]
        super().__init__("查询设置", "\n".join(lines), "主机空=本机；Ctrl+D 选择目录（逗号分隔，留空=根目录全部）；时间空=不限；并发1～2；条数1～100000；超时1～3600秒")

    def field_widgets(self):
        for label, key, options in self.RADIO_FIELDS:
            with Horizontal(classes="settings-row"):
                yield Label(label, classes="settings-label")
                yield RadioSet(*[RadioButton(option, value=(option == self.radio_defaults[key])) for option in options], id="setting-" + key, compact=True)

    def radio_values(self):
        result = {}
        for field, key, options in self.RADIO_FIELDS:
            button = self.query_one("#setting-" + key, RadioSet).pressed_button
            result[field] = str(button.label) if button is not None else options[0]
        return result

    @on(Button.Pressed, "#pick-dirs")
    def pick_button(self):
        self.action_pick_directories()

    def action_pick_directories(self):
        try:
            fields = parse_fields(self.query_one("#editor", TextArea).text, self.KEYS, repeated=("日志目录",), optional=self.OPTIONS)
        except ValueError as exc:
            self.query_one("#error", Static).update(str(exc))
            return
        if not fields["根目录"]:
            self.query_one("#error", Static).update("请先填写根目录，再选择日志目录")
            return
        self.query_one("#error", Static).update("正在发现日志目录…")
        self.run_worker(self.pick_directories(fields["主机"] or None, fields["根目录"], parse_directory_values(fields["日志目录"], fields["根目录"])), exclusive=True, group="dir-pick")

    async def pick_directories(self, host, root, current):
        directories = None
        try:
            value = request("logs", host=host, root=root, timeout=20)
            directories, _, _ = await self.app.controller.logs(value, host)
        except (OSError, ValueError, asyncio.CancelledError) as exc:
            if not isinstance(exc, asyncio.CancelledError):
                self.app.operations.exception("ui.directory_discovery_failed", exc, target=host or "local", root=root)
        if not self.is_mounted or self.app.closing:
            return
        if not directories:
            self.query_one("#error", Static).update("日志目录发现失败：请检查主机、根目录或连接后重试")
            return
        self.query_one("#error", Static).update("")
        selected = await self.app.push_screen_wait(DirectoryDialog(directories, current))
        if selected is not None and self.is_mounted:
            self.app.operations.emit("ui.directory_choice_applied", target=host or "local", root=root, directories=selected)
            editor = self.query_one("#editor", TextArea)
            editor.load_text(replace_directories(editor.text, root, selected))

    def save_text(self, text):
        fields = parse_fields(text, self.KEYS, repeated=("日志目录",), optional=self.OPTIONS)
        fields.update(self.radio_values())
        root = fields["根目录"]
        directories = parse_directory_values(fields["日志目录"], root)
        if not root and not directories:
            raise ValueError("请填写根目录（日志目录留空=全部），或手填日志目录")
        values = dict(host=fields["主机"] or None, mode="root" if root else "dirs", root=root or self.settings["root"], log_dirs=directories, since=fields["开始时间"] or None, until=fields["结束时间"] or None, include_archives=boolean(fields["包含gzip"], "包含gzip"), include_unknown_time=boolean(fields["包含未知时间"], "包含未知时间"))
        mode = fields.get("内容模式", "完整条目" if self.settings.get("result_mode") == "entries" else "命中行")
        if mode not in ("命中行", "完整条目"):
            raise ValueError("内容模式请填写 命中行 / 完整条目")
        values.update(filter_file_dates=boolean(fields.get("按文件日期筛选", "是" if self.settings.get("filter_file_dates", True) else "否"), "按文件日期筛选"), result_mode="lines" if mode == "命中行" else "entries", auto_expand_exceptions=boolean(fields.get("异常自动展开", "是" if self.settings.get("auto_expand_exceptions", True) else "否"), "异常自动展开"), expand_pattern=fields.get("展开正则", self.settings.get("expand_pattern", "")), concurrency=int(fields.get("并发数", self.settings.get("concurrency", 2))))
        if values["concurrency"] not in (1, 2):
            raise ValueError("并发数只能为 1 或 2")
        values.update(max_entries=int(fields.get("返回条数") or self.settings.get("max_entries", 100)), timeout=int(fields.get("超时秒数") or self.settings.get("timeout", 60)))
        if not 1 <= values["max_entries"] <= 100000:
            raise ValueError("返回条数须为 1～100000")
        if not 1 <= values["timeout"] <= 3600:
            raise ValueError("超时秒数须为 1～3600")
        request(host=values["host"], root=root or None, log_dirs=directories or None, query="validation", since=values["since"], until=values["until"], include_archives=values["include_archives"], include_unknown_time=values["include_unknown_time"], timeout=values["timeout"], max_entries=values["max_entries"], **{key: values[key] for key in ("filter_file_dates", "result_mode", "auto_expand_exceptions", "expand_pattern", "concurrency")})
        self.dismiss(values)


class DirectoryDialog(Dialog):
    """勾选根目录下的服务日志目录；Ctrl+S 确认，返回则保持原范围。"""

    DEFAULT_CSS = """
    DirectoryDialog #directories { border: round $primary; background: $surface; }
    DirectoryDialog #directory-hint { height: auto; color: $text-muted; }
    """
    # Enter toggles the highlighted option inside SelectionList; confirm with Ctrl+S.
    BINDINGS = [("ctrl+s", "confirm", "确认")]

    def __init__(self, directories, selected):
        super().__init__()
        self.options, self.chosen = directories, list(selected)

    def compose(self):
        with Vertical():
            yield Label("选择日志目录")
            yield SelectionList(id="directories")
            yield Static("空格/Enter 勾选切换 · Ctrl+S/确定 应用 · Esc 返回不改变范围", id="directory-hint", markup=False)
            yield Static("", id="error", markup=False)
            with Horizontal():
                yield Button("全选", id="all")
                yield Button("清空", id="none")
                yield Button("返回", id="back")
                yield Button("确定", id="apply", variant="primary")

    def on_mount(self):
        listing = self.query_one("#directories", SelectionList)
        chosen = set(self.chosen)
        listing.add_options(("{} · {} 文件 · {:.1f} MiB".format(item["service"], item["files"], item["bytes"] / 1048576), item["path"], item["path"] in chosen) for item in self.options)
        # SelectionList needs an explicit height inside the auto-sized dialog body.
        listing.styles.height = max(4, min(len(self.options) + 2, max(4, self.app.size.height - 12)))
        listing.focus()

    @on(Button.Pressed, "#all")
    def select_all(self):
        self.query_one("#directories", SelectionList).select_all()

    @on(Button.Pressed, "#none")
    def select_none(self):
        self.query_one("#directories", SelectionList).deselect_all()

    def action_confirm(self):
        chosen = list(self.query_one("#directories", SelectionList).selected)
        if not chosen:
            self.query_one("#error", Static).update("至少勾选一个日志目录；Esc 返回保持当前范围")
            return
        self.dismiss(chosen)

    @on(Button.Pressed, "#apply")
    def confirm(self):
        self.action_confirm()

import argparse
import asyncio
import json
import shlex
import signal
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from .config import host_aliases, ssh_config_path
from .controller import Controller, request, sort_key, today_time_range
from .export import export_task
from .operations import OperationLog, log_path, read_records


class DiagnosticParser(argparse.ArgumentParser):
    def error(self, message):
        operations = OperationLog("cli.parser")
        # argparse errors can echo invalid user values; retain the error class
        # and fingerprint, not arbitrary arguments that could contain secrets.
        operations.emit("cli.arguments_invalid", level="warning", reason=message.split(":", 1)[0], text=message)
        operations.close()
        super().error(message)


def parser():
    result = DiagnosticParser(prog="log-search", description="本机与 SSH 日志搜索；查询值必填，时间按源日志墙钟筛选 [since, until)")
    result.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    commands = result.add_subparsers(dest="command", required=True)
    hosts = commands.add_parser("hosts", help="列出 SSH config 中的具体别名")
    hosts.add_argument("--ssh-config")
    hosts.add_argument("--format", choices=("text", "jsonl"), default="text")
    diagnostics = commands.add_parser("diagnostics", help="查看本地操作日志，定位查询与显示问题")
    diagnostics.add_argument("--tail", type=int, default=100, help="最近 1～1000 条事件")
    diagnostics.add_argument("--session-id")
    diagnostics.add_argument("--task-id")
    diagnostics.add_argument("--format", choices=("text", "jsonl"), default="text")
    for name, help_text in (("search", "搜索 traceId / 字面量"), ("context", "读取指定行所属条目和相邻上下文"), ("check", "检查目标能力和日志目录"), ("logs", "列出部署根目录下的服务日志目录")):
        command = commands.add_parser(name, help=help_text)
        target = command.add_mutually_exclusive_group(required=True)
        target.add_argument("--local", action="store_true")
        target.add_argument("--host")
        scope = command.add_mutually_exclusive_group(required=True)
        scope.add_argument("--root", help="部署根目录，自动发现各服务 logs")
        scope.add_argument("--log-dir", action="append", help="指定日志目录，可重复")
        command.add_argument("--ssh-config")
        command.add_argument("--interactive-auth", action="store_true", help="通过真实终端完成 SSH 密码或首次主机确认")
        command.add_argument("--timeout", type=float, default=60 if name == "search" else 15)
        command.add_argument("--format", choices=("text", "jsonl"), default="text")
        if name == "search":
            command.add_argument("--query", required=True)
            command.add_argument("--since")
            command.add_argument("--until")
            command.add_argument("--no-archives", action="store_true")
            command.add_argument("--include-unknown-time", action="store_true")
            command.add_argument("--max-entries", type=int, default=100)
            command.add_argument("--all-file-dates", action="store_true", help="关闭文件名日期筛选，扫描所有归档")
            command.add_argument("--result-mode", choices=("lines", "entries"), default="lines")
            command.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
            command.add_argument("--expand-pattern", default="", help="命中行符合此正则时展开所属条目")
            command.add_argument("--no-exception-expansion", action="store_true")
            command.add_argument("--export", type=Path, help="保存本次已返回结果为 JSONL；拒绝覆盖")
            command.add_argument("--yes", "-y", action="store_true", help="裸跑补默认值时跳过执行确认")
        elif name == "context":
            command.add_argument("--file", required=True)
            command.add_argument("--line", type=int, required=True)
    tui = commands.add_parser("tui", help="启动 Textual 交互界面")
    tui.add_argument("--ssh-config")
    tui.add_argument("--root")
    tui.add_argument("--host")
    return result


def exit_status(done):
    if done is None:
        return 1
    outcome = done.get("outcome")
    if outcome == "cancelled":
        return 130
    if outcome == "timed_out":
        return 124
    if outcome == "failed":
        return 1
    return 0 if done.get("scope_complete") else 3


def display_entry(console, entry):
    console.print(Text("{}  {}  {}  {}:{}".format(entry.get("ts") or "时间未知", entry["service"], entry["level"], entry["file"], entry["start_line"]), style="cyan"))
    for line in entry["preview"]:
        console.print(Text(line))
    console.print()


async def run(args):
    controller = Controller(args.ssh_config, role="cli")
    console, diagnostic = Console(highlight=False), Console(stderr=True, highlight=False)
    controller.operations.emit("cli.command", arguments=vars(args))
    if controller.operations.error:
        diagnostic.print(Text("操作日志不可用：" + controller.operations.error, style="yellow"))
    task = None
    scope_directories = []
    loop = asyncio.get_running_loop()
    try:
        if args.interactive_auth and args.host:
            controller.ssh.authenticate(args.host)
        extra = dict(file=args.file, line=args.line) if args.command == "context" else {}
        if args.command == "search":
            extra.update(filter_file_dates=not args.all_file_dates, result_mode=args.result_mode, concurrency=args.concurrency, expand_pattern=args.expand_pattern, auto_expand_exceptions=not args.no_exception_expansion)
        value = request(args.command, host=args.host, root=args.root, log_dirs=args.log_dir, query=getattr(args, "query", ""), since=getattr(args, "since", None), until=getattr(args, "until", None), include_archives=not getattr(args, "no_archives", False), include_unknown_time=getattr(args, "include_unknown_time", False), max_entries=getattr(args, "max_entries", 100), timeout=args.timeout, **extra)
        task = await controller.start(value, args.host)
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(task.cancel()))
        async for event in task.events():
            if args.format == "jsonl":
                print(json.dumps(event, ensure_ascii=True), flush=True)
            elif event["type"] == "warning":
                diagnostic.print(Text(event.get("message", ""), style="yellow"))
            elif event["type"] == "ready" and args.command == "check":
                console.print(Text("能力检查通过：" + json.dumps(event["capabilities"], ensure_ascii=False)))
                for directory in event["directories"]:
                    console.print(Text(directory))
            elif event["type"] == "ready":
                scope_directories = event["directories"]
            elif event["type"] == "logs":
                for directory in event["directories"]:
                    console.print(Text("{} · {} 文件 · {:.1f} MiB".format(directory["path"], directory["files"], directory["bytes"] / 1048576)))
            elif event["type"] == "context":
                if event.get("source_changed"):
                    diagnostic.print("来源文件已变化", style="yellow")
                for entry in event["entries"]:
                    display_entry(console, entry)
        if args.format == "text":
            for entry in sorted(task.entries, key=sort_key):
                display_entry(console, entry)
            done = task.done or {}
            diagnostic.print(Text("{} · {} 条目 · 范围{} · {}s".format(done.get("outcome", "failed"), done.get("entries", 0), "读取完整" if done.get("scope_complete") else "不完整", done.get("elapsed", "?"))))
            if args.command == "search" and scope_directories:
                shown = "、".join(scope_directories[:3]) + ("…共 {} 个".format(len(scope_directories)) if len(scope_directories) > 3 else "")
                diagnostic.print(Text("扫描范围：{} · 发现 {} 文件 · 实际读取 {} MiB · 跳过符号链接 {}".format(shown, done.get("discovered", 0), round(done.get("bytes_read", 0) / 1048576, 1), done.get("skipped_links", 0))))
            if not done.get("content_complete", True):
                diagnostic.print("部分条目预览已截断", style="yellow")
            if done.get("filter_file_dates"):
                diagnostic.print(Text("按文件名日期选择范围 · 跳过 {} 文件；需要全目录排查时使用 --all-file-dates".format(done.get("skipped_time_files", 0))))
        if getattr(args, "export", None):
            task.log("cli.export_requested", path=str(args.export), entries_count=len(task.entries), format="jsonl")
            with args.export.open("x", encoding="utf-8") as output:
                output.write(export_task(task))
            task.log("cli.export_finished", path=str(args.export), entries_count=len(task.entries))
        code = exit_status(task.done)
        task.log("cli.finished", exit_code=code)
        return code
    except BrokenPipeError:
        controller.operations.emit("cli.output_closed", level="warning")
        if task:
            await task.cancel()
        # Avoid a second BrokenPipeError during interpreter shutdown.
        sys.stdout = open("/dev/null", "w")
        return 130
    except Exception as exc:
        controller.operations.exception("cli.failed", exc)
        raise
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        await controller.close()


COMMANDS = ("search", "context", "check", "logs", "hosts", "diagnostics", "tui")


def expand(argv):
    """A bare --query run means: search this directory, today only. Nothing else is implied."""
    if argv and (argv[0] in COMMANDS or argv[0].startswith("-h") or argv[0] == "--version"):
        return argv
    flags = {arg.split("=", 1)[0] for arg in argv}
    result = ["search"]
    local = not flags & {"--local", "--host"}
    if local:
        result.append("--local")
    # A local working directory has no meaning on the remote host, so remote runs still need an explicit scope.
    if local and not flags & {"--root", "--log-dir"}:
        cwd = Path.cwd()
        if cwd in (Path.home(), Path("/")):
            raise ValueError("当前目录是家目录或根目录，请显式指定 --root 或 --log-dir")
        result += ["--log-dir", str(cwd)]
    if not flags & {"--since", "--until"}:
        result += ["--since", today_time_range()[0], "--until", today_time_range()[1]]
    return result + argv


def preview(args, argv):
    """Echo the resolved run so an adjusted repeat does not require reading argparse defaults."""
    diagnostic = Console(stderr=True, highlight=False)
    # sys.argv[0] is re-runnable for the installed script and the zipapp, but not for python -m.
    launcher = "python3 -m log_search" if Path(sys.argv[0]).name == "__main__.py" else sys.argv[0]
    diagnostic.print(Text("等效命令：" + " ".join(shlex.quote(part) for part in [launcher] + argv)), soft_wrap=True)
    diagnostic.print(Text("生效参数：时间窗 {} ～ {} [起,止) · 上限 {} 条 · 超时 {}s · 结果 {} · 并发 {} · gzip {} · 文件名日期筛选 {}".format(
        args.since, args.until, args.max_entries, args.timeout, "整条" if args.result_mode == "entries" else "单行",
        args.concurrency, "关" if args.no_archives else "开", "关" if args.all_file_dates else "开")))
    if not args.yes and sys.stdin.isatty() and diagnostic.input("执行？ [Y/n] ", markup=False).strip().lower() not in ("", "y", "yes"):
        raise SystemExit(130)


def main():
    command_parser = parser()
    argv = sys.argv[1:]
    try:
        resolved = expand(argv)
    except ValueError as exc:
        command_parser.error(str(exc))
    args = command_parser.parse_args(resolved)
    if resolved != argv:
        preview(args, resolved)
    try:
        if args.command == "diagnostics":
            records = read_records(tail=args.tail, session_id=args.session_id, task_id=args.task_id)
            if args.format == "text":
                print("操作日志：" + str(log_path()))
                if not records:
                    print("没有符合条件的操作记录")
            for record in records:
                print(json.dumps(record, ensure_ascii=True))
            return
        if args.command == "hosts":
            operations = OperationLog("cli.hosts")
            try:
                aliases = host_aliases(ssh_config_path(args.ssh_config))
                operations.emit("cli.hosts", ssh_config=ssh_config_path(args.ssh_config), aliases_count=len(aliases))
                for alias in aliases:
                    print(json.dumps(dict(host=alias)) if args.format == "jsonl" else alias)
            except (OSError, ValueError) as exc:
                operations.exception("cli.hosts_failed", exc)
                raise
            finally:
                operations.close()
            return
        if args.command == "tui":
            try:
                from .tui import LogSearchApp
            except ImportError as exc:
                raise ValueError("请安装 TUI extra：pip install 'product-log-search[tui]'") from exc
            LogSearchApp(ssh_config=args.ssh_config, root=args.root, host=args.host).run()
            return
        code = asyncio.run(run(args))
    except (OSError, ValueError) as exc:
        command_parser.error(str(exc))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)

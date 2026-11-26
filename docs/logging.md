# 操作日志与问题定位

CLI / TUI 自动记录本地 JSONL 操作日志：`$XDG_STATE_HOME/log-search/operations.jsonl`，默认 `~/.local/state/log-search/operations.jsonl`。单文件 5 MiB，保留 4 个备份；目录 0700、文件 0600。使用 CLI `diagnostics` 查看记录并按会话 / 任务 ID 筛选。

```bash
log-search diagnostics --tail 200
log-search diagnostics --task-id TASK_ID --tail 1000 --format jsonl
```

记录实际目标、范围、时间、能力、扫描进度、返回条数、渲染行数、程序位置 / 源码指纹，以及取消、异常和子进程退出。UUID traceId 保留值与空白信息；普通关键词 / 文本过滤词只记指纹和长度。不保存业务日志正文、复制内容、认证凭据或远端脚本。不污染查询 stdout，无需远端落盘；日志不可写不会阻断搜索。操作日志的设计与定位流程见仓库源码注释与 `log-search diagnostics` 说明。

## 常用定位套路

| 现象 | 查法 |
| --- | --- |
| 命中比预期少 | `diagnostics --task-id ...`，看 `phase` 事件的 `skipped_time_files` 与 `skipped_links`；或直接 `log-search logs --local --root ...` 核对候选目录 |
| 范围标记不完整 | 同上看 `unreadable_files`、`unsupported_files`、`source_changed` |
| 远端连不上 | 看 `ssh.*` 事件；先跑 `log-search check --host ...` 再跑 `search` |
| 结果口径存疑 | `session.start` 里的 `executable`、`package_path`、`source_sha256` 指明跑的是仓库态还是单文件态（归档态 `source_sha256` 为 `null`） |

## 一个已知副作用

操作日志本身是普通文件。若把 `$XDG_STATE_HOME` 指进被扫描的目录，检索会读到自己正在追加的日志并报“扫描期间日志发生变化”（退出码 3）。默认位置 `~/.local/state/log-search` 通常不在日志范围内，无需处理。

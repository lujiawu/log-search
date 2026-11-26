# log-search

Python CLI 与 Textual 日志检索工具，支持本机及系统 OpenSSH 远端查询。默认搜索各服务的 `logs/`，包括业务服务、历史目录、含 NUL 的滚动分片、无扩展名文件和 gzip。

## 安装

本机 Linux、Python 3.10+、OpenSSH（远端模式）；查询目标需要 Python 3.8+、ripgrep 和 gzip（可用 `--no-archives` 关闭压缩日志）。原生 gzip/rg 筛选命中文件后，由 Python 标准库提取条目。不安装远端应用，不修改服务。

在仓库根目录：

```bash
uv sync --extra tui --extra test
uv run log-search --help
```

或单独创建环境：

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[tui,test]'
.venv/bin/log-search --help
```

仅安装基本包时 CLI 不需要 Textual。新工具被排除在产品部署 ZIP 外，不改变现有 Web、Bash wrapper 或 Compose 配置。可执行程序独立打包留到后续阶段。

## CLI

```bash
# 本机：部署根目录
log-search search --local --root /srv/app-logs --query 'TRACE'

# 本机：下载的多个日志目录，路径可以含空格
log-search search --local \
  --log-dir '/tmp/incident/app-beta/logs' \
  --log-dir '/tmp/incident/app-alpha/logs' --query 'TRACE'

# 时间为源日志墙钟时间，采用 [since, until)
log-search search --local --root /srv/app-logs --query 'TRACE' \
  --since '2026-01-01 10:00:00' --until '2026-01-01 11:00:00'

# 当前用户的自定义 SSH 配置
log-search hosts --ssh-config ~/.ssh/config
log-search check --host demo --root /srv/app-logs \
  --ssh-config ~/.ssh/config
log-search search --host demo --root /srv/app-logs --query 'TRACE' \
  --ssh-config ~/.ssh/config --format jsonl

# 若需密码、密钥口令或首次主机确认，使用真实终端交互
log-search search --host demo --root /srv/app-logs --query 'TRACE' \
  --ssh-config ~/.ssh/config --interactive-auth

# 当前文件指定行所属条目及前后各两个条目，结果有字节上限
log-search context --local --root /srv/app-logs \
  --file app-beta/logs/system.log --line 123

# 保存 JSONL；拒绝覆盖已有文件
log-search search --local --root /srv/app-logs --query 'TRACE' \
  --export /tmp/trace-results.jsonl
```

查询值必填，搜索前自动清理首尾空白，保留中间空格，之后精确字面量匹配；清理后为空则报错。可用 `--no-archives` 排除 gzip、`--include-unknown-time` 保留无法判断时间范围的条目、`--timeout 120` 调整时限、`--max-entries 2000` 调整结果上限。

人读模式最终按时间输出，进度与摘要使用 stderr；JSONL 模式持续输出 ready/progress/entry/warning/done。Ctrl+C 取消并保留已返回结果。退出码：0 完整扫描（含零命中）、1 执行失败、2 参数错误、3 范围不完整 / 达到上限、124 超时、130 取消。条目预览截断单独标记，不等同于范围没扫完。

## TUI

```bash
log-search tui --root /srv/app-logs
log-search tui --host demo --root /srv/app-logs \
  --ssh-config ~/.ssh/config
```

主屏由一行查询、一条分隔线、日志列表和一行状态组成。80×24 终端中日志区占 21 行，查询行使用背景色并通过细线与日志分开。输入查询后 Enter 搜索；Ctrl+F 回查询框，Tab 切换焦点。

顶部远端主机显示 `user@地址`，例如 `admin@example.com`，通过本机 `ssh -G` 解析 SSH 配置，不建立连接；配置使用域名时显示配置中的域名，不额外做 DNS 查询。点击主机打开设置，编辑框也预填同一个 `user@地址`，可直接修改用户名或 IP；保存后实际 SSH 连接使用该字段的新值。取消编辑保持原连接目标，解析失败时保留原输入。本机显示“本机”。

单击只选择日志，**双击或 Enter 打开全屏详情，Esc 返回原位置**。详情文本只读，支持鼠标拖选和 Shift+方向键选择；“上下文”按需读取来源文件。时间、服务和级别保持可见，同一天的日期在状态行显示；跨日期结果使用完整时间。

跨文件完整内容一致的结果在列表中聚合为一行，标记 `×N 来源`，详情保留所有文件与行号。同一文件中不同位置、截断、边界未完成或来源变化的条目不折叠。原始命中保留；聚合不代表推断为同一次业务事件。

复制方式：

- 详情中按 Ctrl+C / Ctrl+Shift+C，或点击“复制”：有选择时复制选中文字，无选择时复制当前详情。
- Ctrl+Y 复制当前详情全部内容。若已加载上下文，复制的是当前可见的条目与上下文；截断记录保留标记。
- 优先使用本机 `wl-copy`（Wayland）、`xclip` / `xsel`（X11）；这些工具为可选依赖。缺少或失败时回退 OSC52，提示“已发送终端复制请求”。终端可能禁用或限制 OSC52，提示不代表已确认系统剪贴板写入成功，此时可用 CLI `--export` 落盘。

已移除 `?` 操作菜单、底部操作按钮、结果筛选弹窗（`/`）和 TUI 导出（导出仅 CLI 提供，见上文 `--export`）。设置直接点击顶部主机。表单改动不重新扫描；现有结果仍绑定原任务。纯时间查询仍不支持。CLI 帮助使用 `log-search --help`，操作诊断使用 `log-search diagnostics`。

查询设置使用一个可编辑 TextArea，直接修改 `字段 = 值`。查询设置顶部另有三行单选项：**内容模式、包含gzip、异常自动展开**（点选或方向键+回车，其余开关仍填文本 `是 / 否`）。Enter 换行，**Ctrl+S 或“保存”应用，Esc 放弃**；错误显示在编辑区下方，保留草稿。设置保存后回到查询框，直接 Enter 查询。Tab 切换到保存 / 返回，Ctrl+C 复制选中文字并保留编辑框。

TUI 每次启动默认查询本机日历当天：当天 00:00 到次日 00:00，按日志原始时间 `[开始, 结束)` 匹配。下面以 2026-11-26 为例：

```text
主机 = demo开发环境
根目录 = /srv/app-logs
日志目录 =
开始时间 = 2026-11-26 00:00:00
结束时间 = 2026-11-27 00:00:00
包含gzip = 是
包含未知时间 = 否
返回条数 = 100
超时秒数 = 60
```

主机留空表示本机；填根目录后用“选择目录”勾选服务（留空=全部）。`日志目录` 支持逗号分隔多个目录（半角逗号会被当作分隔符，不能出现在路径中），勾选结果按根目录显示为相对子目录（如 `app-beta/logs, nacos/logs`），重复写多行 `日志目录 = /path` 仍兼容，路径无需引号。**查询历史 traceId 时改日期，或将开始、结束时间的值都清空以查询全部时间**；清空后再次打开仍保留空值。CLI 的默认时间范围仍为不限。

直接填写 `user@IP` 时，OpenSSH 按这个目标匹配 SSH config；仅匹配旧别名的端口、密钥或跳板配置不会自动继承，其他目标所需参数可在 SSH config 中配置。

返回条数默认 100、上限 100000；超时秒数默认 60、上限 3600；两者留空按上一次设置值，达到条数或超时即以“部分结果 / 已超时”收尾。导出仅 CLI 提供：`--export` 保存本次已返回结果为 JSONL，或 `--format jsonl` 重定向输出。

扫描设置文本区保留以下可编辑项（内容模式、包含gzip、异常自动展开已改为顶部单选项）：

```text
按文件日期筛选 = 是
展开正则 =
并发数 = 2
```

默认依据文件名日期选择与查询区间相交的日文件，日期未知保留；头部时间只作辅助排序，不能证明整文件范围。日期筛选可能跳过命名日期之外的跨日内容，需全面排查时改为“否”。状态提示筛选和跳过数量，完成指所选文件范围完成。并发根据候选任务数自动取不超过2路；配置仅允许1或2，定位、解压、提取共用额度。

**按服务勾选日志目录**（设置在查询设置内）。点击顶部主机打开查询设置，根目录照常填写；点 **“选择目录”** 按钮（或 Ctrl+D），工具用一次只读 `logs` 操作枚举该主机根目录下各服务的 `logs`（服务名、文件数、占用），在 `SelectionList` 里勾选，**空格/Enter 切换、Ctrl+S 或“确定”应用、Esc 放弃**；至少勾选一个。勾选结果写回设置草稿的 `日志目录` 行（根目录内显示为相对子目录，逗号分隔，如 `app-beta/logs, nacos/logs`），可在保存前继续核对文本；**Ctrl+S 保存设置才生效**，Esc 关闭设置则本次勾选一并放弃。日志目录留空 = 搜索整个根目录；根目录留空 + 手填日志目录 = 散装目录后备（同 CLI `--log-dir`）。勾选后提交 `root` + `log_dirs`，结果只来自勾选服务、仍按根目录显示相对路径；范围之外的记录不会出现，不能称为全平台链路。勾选按主机记忆（偏好 `picked`），重开设置预填；枚举失败在设置内提示，不改当前范围。勾选目录后仍叠加日期筛选与最多两路扫描。顶部主机后显示 `· N目录`。CLI 用 `log-search logs --root ...` 查看候选目录，`--log-dir` 可与 `--root` 并用限定范围。

命中行模式对每个文件只跑一遍原生 `rg -n -B 32`，直接输出“行号:整行”加至多 32 行前置上下文，Python 不重读文件；命中行向上扫描上下文继承条目头时间/级别、识别多行敏感字段开头（块中段命中整行遮蔽）。上下文窗口之外的深续行（如超过 32 行的多行 JSON 值行）按“时间未知”处理，配合包含未知时间开关过滤。明确异常特征或命中“展开正则”的行是唯一还会读文件的复杂路径：先扣下该行，rg 结束后按行号走完整条目提取，被展开条目覆盖的行命中不再输出。单行超出 16 KiB 截断并标记，截断行不参与聚合。展开正则只检查有界行预览及命中片段，每行最多0.5秒，超限标记部分结果。查询本身仍是字面量，展开正则不扩大关键词搜索范围。“完整条目”走同一趟 rg（追加 `-A 16`）：条目头与下一条目头都在窗口内时直接从上下文拼装，指纹与流式解析一致、不再二次读文件；长堆栈、文件末尾未闭合条目、截断超长行等回退整文件重读，预览限额及截断标记保留。单行与完整条目分别聚合，不把相同行等同为完整条目相同。

CLI 对应 `--concurrency 2 --result-mode lines --expand-pattern '规则'`；`--all-file-dates` 关闭日期文件选择，`--no-exception-expansion` 关闭异常自动展开，`--result-mode entries` 使用完整条目模式。

SSH agent / 密钥可以直接连接；需要交互认证时，先在终端使用同一 SSH config 和目标完成认证。保留主机密钥检查，不记录密码。SSH config 的 TTY/RemoteCommand 对程序查询被覆盖，其他连接参数继续由 OpenSSH 解析。

快捷键：Ctrl+F 查询，↑↓ / j k 浏览，r 重查，x 取消，小写 q 退出。输入框正常接收这些字母和问号；`?`、`e`、`/` 和 Ctrl+P 都不打开菜单或弹窗。设置内 Ctrl+D 选择日志目录。**Ctrl+C 在详情中复制；主屏搜索中取消、空闲时不退出**；退出使用非输入状态的小写 q，仍有任务时会先取消清理。

主题可在偏好文件中设置，详情支持换行开关；偏好位于 `$XDG_CONFIG_HOME/log-search/config.json` 或 `~/.config/log-search/config.json`。示例：

```json
{
  "ssh_config": "~/.ssh/config",
  "theme": "textual-dark",
  "wrap": true,
  "roots": {"local": "/srv/app-logs"},
  "keys": {"search": "ctrl+s", "settings": "f2"}
}
```

keys 支持 search/filter/cancel/quit/query/settings 的额外绑定，旧 help/operations/export 绑定忽略。旧配置的 layout / detail_ratio 不再影响布局。原始日志、查询历史和密码不写入偏好；`picked` 仅记录各主机勾选的日志目录路径，不含业务正文。业务正文只在用户显式导出或复制时向文件 / 剪贴板输出。操作日志另外记录有界的查询特征与执行元数据。

## 完整性与限制

- 文件名日期和头部时间只用于处理优先级，不据此假设时间单调或排除文件。普通文件由 rg 定位候选文件与命中行号；gzip 原生流式定位命中行并检查解压状态，再提取命中条目。未命中条目只做轻量边界扫描，仍保留多行与脱敏规则。行号预算耗尽或文件变化时回退流式匹配；不跳过同名普通/gzip文件。
- 完整内容指纹覆盖条目原始字节，`capture_complete` 表示按现有条目边界规则读取结束，不能证明应用调用栈全部输出；预览仍有原限额，截断结果明确标记。
- 不跟随符号链接、不读取非普通文件；被跳过的链接和读取错误会使范围标记为不完整。
- 时间按条目首行解析，未知时间单独计数；时钟未校正，不据此推断父子调用或绝对因果关系。
- 活跃日志没有一致性快照；读取期间变化会报告，详情重读会校验文件身份。查询中途新增文件可能不属于本次发现范围。
- 默认 100 条、60 秒、32 MiB 结果预算；预览与上下文也有上限。达到上限、超时、损坏压缩包或连接中断不会显示成完整零命中。命中行模式早停：数到上限即终止 rg，提示“达到上限”，请缩小时间窗或提高 `--max-entries`。
- 共享展示和导出对 Authorization/Cookie/token/password 等字段保守脱敏，无法覆盖所有业务秘密；共享前仍需检查。当前没有“显示未脱敏原文”入口。
- 上下文最多前后各两条，有字节截断提示；当前没有分段连续读取任意超大条目的入口。
- 首版没有实时 tail、多主机联合搜索、正则 / 组合查询、全文索引、自动重试和跨平台独立程序。

## 操作日志与问题定位

CLI / TUI 自动记录本地 JSONL 操作日志：`$XDG_STATE_HOME/log-search/operations.jsonl`，默认 `~/.local/state/log-search/operations.jsonl`。单文件 5 MiB，保留 4 个备份；目录 0700、文件 0600。使用 CLI `diagnostics` 查看记录并按会话 / 任务 ID 筛选。

```bash
log-search diagnostics --tail 200
log-search diagnostics --task-id TASK_ID --tail 1000 --format jsonl
```

记录实际目标、范围、时间、能力、扫描进度、返回条数、渲染行数、程序位置 / 源码指纹，以及取消、异常和子进程退出。UUID traceId 保留值与空白信息；普通关键词 / 文本过滤词只记指纹和长度。不保存业务日志正文、复制内容、认证凭据或远端脚本。不污染查询 stdout，无需远端落盘；日志不可写不会阻断搜索。操作日志的设计与定位流程见仓库源码注释与 `log-search diagnostics` 说明。

## 验证

```bash
uv run --extra tui --extra test pytest tests -q
uv run python scripts/benchmark.py --size-mib 3072
```

测试包含真实 rg/gzip、Textual Pilot、模拟 SSH 引导，以及有 sshd 时用临时密钥和回环监听执行的真实 OpenSSH 测试。后者无需业务服务器；环境不能运行 sshd 时明确跳过。

benchmark 生成并清理临时合成数据，不读取现场日志，不清空系统缓存。测量结果只能作为指定硬件与数据分布下的参考。

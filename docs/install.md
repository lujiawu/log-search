# 安装

## 依赖

本机 Linux、Python 3.10+、OpenSSH（远端模式）；查询目标需要 Python 3.8+、ripgrep 和 gzip（可用 `--no-archives` 关闭压缩日志）。原生 gzip/rg 筛选命中文件后，由 Python 标准库提取条目。不安装远端应用，不修改服务。

## 从仓库安装

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

仅安装基本包时 CLI 不需要 Textual：`rich` 是唯一硬依赖，`log-search tui` 才加载 Textual。

## 免安装单文件

不想在跳板机上建虚拟环境，见 [packaging.md](packaging.md)：`sh scripts/build.sh` 产出 `dist/log-search`，`scp` 过去 `chmod +x` 直接运行。

## 边界

新工具被排除在产品部署 ZIP 外，不改变现有 Web、Bash wrapper 或 Compose 配置。

## 验证

```bash
uv run --extra tui --extra test pytest tests -q
uv run python scripts/benchmark.py --size-mib 3072
```

测试包含真实 rg/gzip、Textual Pilot、模拟 SSH 引导，以及有 sshd 时用临时密钥和回环监听执行的真实 OpenSSH 测试。后者无需业务服务器；环境不能运行 sshd 时明确跳过。

benchmark 生成并清理临时合成数据，不读取现场日志，不清空系统缓存。测量结果只能作为指定硬件与数据分布下的参考。

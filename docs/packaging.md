# 单文件分发

```bash
sh scripts/build.sh    # 产物 dist/log-search，约 2.6 MB
```

依赖全是纯 Python，打包不需要编译器。`scp` 到跳板机后 `chmod +x` 直接运行；目标机仍需 Python 3.10+ 及 rg、gzip、ssh。

## 构建过程

`scripts/build.sh` 做四件事：

1. `uv pip install --target build/app '.[tui]'` —— 依赖清单取自 `pyproject.toml`，脚本里不抄版本号。
2. 拷入 `src/log_search`，删除所有 `__pycache__`（zipimport 会优先使用 magic 数匹配的 `.pyc`，陈旧 pyc 是静默隐患）。
3. `python3 -m zipapp --compress --main 'log_search.cli:main' --python '/usr/bin/env python3'`。
4. 用产物自身跑一次本机检索作自检：归档态跑不通就不该留下 `dist/`。

## 归档形态的两个坑（已在代码里处理）

worker 要在子进程里跑，而 zip 归档不是文件系统，所以：

- **不能 `open` 归档内的 `.py`**：`Path(worker.__file__).read_bytes()` 会抛 FileNotFoundError。远端 SSH 引导改用 `worker.__loader__.get_source(worker.__name__)`，`SourceFileLoader` 与 `zipimporter` 都实现该接口，源码态与归档态共用一段代码。见 `transport.py` 的 `worker_source()`。
- **不能把归档内的 `.py` 当主脚本执行**：`python3 /x/app.pyz/log_search/worker.py` 起不来。本机 worker 子进程改为把归档路径注入子进程 `sys.path` 再 `import`，`ARCHIVE` 在源码态是 `src/`，归档态是 `.pyz` 本身。见 `transport.py` 的 `local_worker()`。

顺带：归档态没有可读的源码目录，`operations.jsonl` 的 `source_sha256` 记 `null`，不留一个看着正常的假哈希。

## 为什么不用 PyInstaller

目标机有 python3 时它是净亏：30～80 MB、`--onefile` 每次启动解压、绑 glibc 与架构，而且 frozen 态 `sys.executable` 是应用 exe 本身，`sys.executable -c <代码>` 会被自己的 argparse 吃掉，得额外处理。zipapp 态 `sys.executable` 仍是真解释器，这类代码一行不用改。

## 未验证

只在构建机的 Python 3.14 上验证过运行；更低解释器（3.10～3.13）跑同一归档未测，语法向下兼容是假设不是事实。Windows / macOS 未测。

import shutil

import pytest

from log_search import worker

pytestmark = pytest.mark.skipif(not shutil.which("awk"), reason="exception-stats 需要 awk")

LOG = """2026-01-01 10:00:00 INFO start
2026-01-01 10:01:00 ERROR java.lang.RuntimeException: boom
    at com.foo.Bar.baz(Bar.java:42)
    at com.foo.Qux.run(Qux.java:7)
2026-01-01 10:02:00 ERROR java.lang.RuntimeException: boom again
    at com.foo.Bar.baz(Bar.java:99)
    at com.foo.Qux.run(Qux.java:7)
2026-01-01 10:03:00 ERROR plain failure no stack
2026-01-01 10:04:00 INFO done
"""


def run(path, **options):
    events = []
    request = dict(protocol=1, action="exception-stats", scope={"log_dirs": [str(path)]}, **options)
    worker.execute(request, events.append)
    return [e for e in events if e["type"] == "exception_stats"], events


def test_exception_stats_counts_and_stack_aggregation(tmp_path):
    (tmp_path / "app.log").write_text(LOG)
    stats, events = run(tmp_path)
    assert len(stats) == 1
    s = stats[0]
    assert s["total_entries"] == 5
    assert s["exception_entries"] == 3
    assert s["unstacked_exceptions"] == 1
    assert s["grouped_exceptions"] == 2
    assert s["exception_rate"] == round(3 / 5, 6)
    assert s["groups_total"] == 1
    group = s["groups"][0]
    assert group["exception_type"] == "java.lang.RuntimeException"
    assert group["count"] == 2
    # 行号差异被归一化，两条归入同一组；关键帧保留
    assert group["key_frames"] == ["com.foo.Bar.baz", "com.foo.Qux.run"]
    assert group["first_seen"] == "2026-01-01 10:01:00"
    assert group["last_seen"] == "2026-01-01 10:02:00"
    # 样本只给跳转坐标，不给预览：详情交给 search/context
    assert group["sample"]["file"].endswith("app.log")
    assert (group["sample"]["start_line"], group["sample"]["end_line"]) == (2, 4)
    assert group["sample"]["ts"] == "2026-01-01 10:01:00"
    assert "preview" not in group["sample"]
    assert events[-1]["type"] == "done" and events[-1]["outcome"] == "completed"


def test_exception_stats_root_scope_accepts_any_subdirectory(tmp_path):
    logs = tmp_path / "deploy" / "svc" / "logs"
    logs.mkdir(parents=True)
    (logs / "app.log").write_text(LOG)
    root = str(tmp_path / "deploy")
    for directory in ("svc", "svc/logs"):
        events = []
        worker.execute(dict(protocol=1, action="exception-stats",
                            scope={"root": root, "log_dirs": [str(tmp_path / "deploy" / directory)]}), events.append)
        stats = [event for event in events if event["type"] == "exception_stats"][0]
        assert (stats["total_entries"], stats["exception_entries"]) == (5, 3)
    # 越出 root 的目录在 validate 阶段被拒
    events = []
    worker.execute(dict(protocol=1, action="exception-stats",
                        scope={"root": root, "log_dirs": [str(tmp_path)]}), events.append)
    assert events[-1]["outcome"] == "failed"
    assert not any(event["type"] == "exception_stats" for event in events)


def test_exception_stats_tab_indented_stack(tmp_path):
    (tmp_path / "app.log").write_text(
        "2026-01-01 10:00:00 ERROR java.lang.IllegalStateException: x\n"
        "\tat com.foo.Bar$Inner.baz(Bar.java:1)\n"
        "\tat com.foo.Qux.lambda$run$0(Qux.java:2)\n"
        "\tat com.foo.Qux.run(Qux.java:3)\n")
    s, _ = run(tmp_path)
    group = s[0]["groups"][0]
    assert group["exception_type"] == "java.lang.IllegalStateException"
    # 制表符缩进走 awk 快路径，$ 与 <> 必须保留，只取前 3 帧
    assert group["key_frames"] == ["com.foo.Bar$Inner.baz", "com.foo.Qux.lambda$run$0", "com.foo.Qux.run"]


def test_exception_stats_time_window_and_unknown_time(tmp_path):
    (tmp_path / "app.log").write_text(LOG)
    (tmp_path / "unknown.log").write_text("ERROR com.foo.Boom: x\n")
    s, _ = run(tmp_path, since="2026-01-01 10:02:00", until="2026-01-01 10:05:00")
    stats = s[0]
    # 窗口 [10:02,10:05): 10:02 RuntimeException + 10:03 无堆栈 ERROR + 10:04 INFO；未知时间默认排除
    assert stats["total_entries"] == 3
    assert stats["exception_entries"] == 2
    assert stats["unstacked_exceptions"] == 1
    s2, _ = run(tmp_path, since="2026-01-01 10:02:00", until="2026-01-01 10:05:00", include_unknown_time=True)
    # 未知时间条目在窗口下可选纳入；它含 EXCEPTION → 计为异常
    assert s2[0]["total_entries"] == 4
    assert s2[0]["exception_entries"] == 3


def test_exception_stats_top_limit(tmp_path):
    (tmp_path / "app.log").write_text(LOG)
    stats, _ = run(tmp_path, top=1)
    assert stats[0]["top"] == 1 and len(stats[0]["groups"]) == 1 and stats[0]["groups_total"] == 1
    # top=0 被校验拒绝，不产出空输出的假成功
    rejected, events = run(tmp_path, top=0)
    assert not rejected and events[-1]["outcome"] == "failed"


def test_exception_stats_invalid_since_rejected(tmp_path):
    (tmp_path / "app.log").write_text(LOG)
    events = []
    worker.execute(dict(protocol=1, action="exception-stats", scope={"log_dirs": [str(tmp_path)]}, since="2026-01-01 10:00:00", until="2026-01-01 10:00:00"), events.append)
    assert events[-1]["outcome"] == "failed"
    assert not any(e["type"] == "exception_stats" for e in events)

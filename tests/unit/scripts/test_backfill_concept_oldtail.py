"""oldtail 增量守卫 (--skip-existing) — 08-29 统筹派单卡

背景: 脚本非增量 (每次从 2024-01-02 全量重跑 561 天) 且 flock_guard 持锁
整个脚本时长, 挂 cron 造成晨间 mass-skip 死循环。本卡给脚本加增量守卫,
使未来可安全重挂 cron。

验证三语义:
① 默认行为不变 (不加 flag = 全量重跑现状);
② --skip-existing: 完成一天才原子记进度, 只跳有完成标记的天;
   中途崩溃留下的部分写入天必须重跑 (不能只看「有行就跳」);
③ 打印 skip/redo 天数统计。

场景: fake pipeline + 临时库构造「部分天已完成 + 部分天部分写入 + 崩溃重启」。
不真调网络, 无生产写。
"""
import sqlite3

from scripts.backfill_concept_oldtail import (
    load_available_dates,
    load_completed_days,
    mark_day_done,
    run_backfill,
)


class FakePipeline:
    """可编程假管道: 指定哪些日期成功, 每成功一天写一行 concept_daily."""

    def __init__(self, fail_dates=()):
        self.fail_dates = set(fail_dates)
        self.calls = []

    def run(self, input):
        self.calls.append(input.date)
        if input.date in self.fail_dates:
            class R:
                status = "failed"
                errors = ["fake fail"]
            return R()
        conn = sqlite3.connect(input.db.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS concept_daily ("
            "code TEXT, date TEXT, concept_name TEXT, change_pct REAL, "
            "source TEXT DEFAULT '', PRIMARY KEY (code, date, concept_name))")
        conn.execute(
            "INSERT OR REPLACE INTO concept_daily VALUES (?,?,?,?,?)",
            ("000001", input.date, "概念甲", 1.0, "zzshare_backfill"))
        conn.commit()
        conn.close()
        class R:
            status = "success"
            errors = []
        return R()


class FakeDB:
    def __init__(self, path):
        self.db_path = path


def _mk_dates_csv(tmp_path, dates):
    csv = tmp_path / "probe.csv"
    lines = ["date,rank15,rank17"]
    for d in dates:
        lines.append(f"{d},398,235")
    csv.write_text("\n".join(lines))
    return str(csv)


def _mk_conn(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE concept_daily (code TEXT, date TEXT, concept_name TEXT, "
        "change_pct REAL, source TEXT DEFAULT '', "
        "PRIMARY KEY (code, date, concept_name))")
    conn.commit()
    return conn, str(path)


# ---------------------------------------------------------------- ② 完成标记

def test_mark_and_load_completed_roundtrip(tmp_path):
    """mark_day_done 原子记一天; load_completed_days 只认完成标记."""
    conn, path = _mk_conn(tmp_path)
    mark_day_done(conn, "2025-01-02")
    assert load_completed_days(conn) == {"2025-01-02"}
    mark_day_done(conn, "2025-01-03")
    assert load_completed_days(conn) == {"2025-01-02", "2025-01-03"}


def test_partial_writes_do_not_count_as_done(tmp_path):
    """核心语义: 只有行没有标记 → 不算完成, 须重跑 (防「有行就跳」)."""
    conn, path = _mk_conn(tmp_path)
    conn.execute(
        "INSERT INTO concept_daily VALUES ('000001','2025-01-02','概念',1.0,'x')")
    conn.commit()  # 模拟崩溃重启: 写了一半没来得及 mark_day_done
    assert load_completed_days(conn) == set()  # 不得把 2025-01-02 算进去


# ------------------------------------------------------------ ① 默认全量语义

def test_default_runs_all_days_even_if_marked(tmp_path, monkeypatch):
    """默认 (不加 flag): 即使有完成标记也全量重跑, 现状语义不变."""
    dates = [f"2025-01-0{i}" for i in range(1, 6)]  # 5 天
    monkeypatch.setattr(
        "scripts.backfill_concept_oldtail.PROBE_CSV", _mk_dates_csv(tmp_path, dates))
    conn, path = _mk_conn(tmp_path)
    # 预置: 前两天已标记完成 + 有行
    for d in dates[:2]:
        mark_day_done(conn, d)
        conn.execute(
            "INSERT INTO concept_daily VALUES ('000001',?,?,1.0,'x')", (d, "概念"))
    conn.commit()
    conn.close()

    pipe = FakePipeline()
    stats = run_backfill(db_path=path, pipe=pipe, skip_existing=False, sleep_s=0)
    assert len(pipe.calls) == 5  # 全部重跑, 标记不影响
    assert stats["redo"] == 5 and stats["skip"] == 0


# ------------------------------------------------------------ ② skip 语义

def test_skip_existing_skips_completed_days_only(tmp_path, monkeypatch):
    """--skip-existing: 跳过完成标记天; 部分写入天与全空天都重跑."""
    dates = [f"2025-01-0{i}" for i in range(1, 6)]  # d1..d5
    monkeypatch.setattr(
        "scripts.backfill_concept_oldtail.PROBE_CSV", _mk_dates_csv(tmp_path, dates))
    conn, path = _mk_conn(tmp_path)
    # d1, d2: 完整完成 (标记+行)
    for d in dates[:2]:
        mark_day_done(conn, d)
        conn.execute(
            "INSERT INTO concept_daily VALUES ('000001',?,?,1.0,'x')", (d, "概念"))
    # d3: 部分写入天 — 有行但无标记 (崩溃重启现场)
    conn.execute(
        "INSERT INTO concept_daily VALUES ('000001',?,?,1.0,'x')", (dates[2], "概念"))
    conn.commit()
    conn.close()

    pipe = FakePipeline()
    stats = run_backfill(db_path=path, pipe=pipe, skip_existing=True, sleep_s=0)
    assert sorted(pipe.calls) == [dates[2], dates[3], dates[4]]  # d3,d4,d5 重跑
    assert stats["skip"] == 2 and stats["redo"] == 3
    # 重跑完成后 d3~d5 也应被标记 (断点续跑链闭合)
    conn = sqlite3.connect(path)
    assert load_completed_days(conn) == set(dates)
    conn.close()


def test_resume_after_crash_continues_from_progress(tmp_path, monkeypatch):
    """崩溃重启两段式: 第一段跑 2 天标记 2 天, 第二段 --skip-existing 续跑余下."""
    dates = [f"2025-01-0{i}" for i in range(1, 7)]  # 6 天
    monkeypatch.setattr(
        "scripts.backfill_concept_oldtail.PROBE_CSV", _mk_dates_csv(tmp_path, dates))
    conn, path = _mk_conn(tmp_path)
    conn.close()

    # 第一段: 崩溃前完成 d1,d2 (跑 2 天就断)
    pipe1 = FakePipeline()
    run_backfill(db_path=path, pipe=pipe1, skip_existing=True, sleep_s=0, limit=2)
    assert len(pipe1.calls) == 2

    # 第二段: 崩溃重启 --skip-existing
    pipe2 = FakePipeline()
    stats = run_backfill(db_path=path, pipe=pipe2, skip_existing=True, sleep_s=0)
    assert len(pipe2.calls) == 4  # 只跑余下 4 天
    assert stats["skip"] == 2 and stats["redo"] == 4
    conn = sqlite3.connect(path)
    assert load_completed_days(conn) == set(dates)  # 全链闭合
    conn.close()


def test_failed_day_not_marked_and_rerun_next_time(tmp_path, monkeypatch):
    """失败日不记标记, 下次 --skip-existing 重跑 (失败天不算完成)."""
    dates = [f"2025-01-0{i}" for i in range(1, 4)]
    monkeypatch.setattr(
        "scripts.backfill_concept_oldtail.PROBE_CSV", _mk_dates_csv(tmp_path, dates))
    conn, path = _mk_conn(tmp_path)
    conn.close()

    pipe1 = FakePipeline(fail_dates={dates[1]})  # d2 失败
    stats1 = run_backfill(db_path=path, pipe=pipe1, skip_existing=True, sleep_s=0)
    assert stats1["redo"] == 3

    pipe2 = FakePipeline()  # 修复后全成功
    stats2 = run_backfill(db_path=path, pipe=pipe2, skip_existing=True, sleep_s=0)
    assert stats2["skip"] == 2 and stats2["redo"] == 1  # 只补 d2
    assert pipe2.calls == [dates[1]]


# ---------------------------------------------------------------- ③ 统计打印

def test_stats_printed_to_stdout(tmp_path, monkeypatch, capsys):
    """skip/redo 统计必须打印 (③: 打印 skip/redo 天数统计)."""
    dates = [f"2025-01-0{i}" for i in range(1, 4)]
    monkeypatch.setattr(
        "scripts.backfill_concept_oldtail.PROBE_CSV", _mk_dates_csv(tmp_path, dates))
    conn, path = _mk_conn(tmp_path)
    # 标记 + 有行 (完整天, 不触发漂移守卫 — 漂移行为由 test_guard_improvements 钉)
    mark_day_done(conn, dates[0])
    conn.execute(
        "INSERT INTO concept_daily VALUES ('000001',?,?,1.0,'x')", (dates[0], "概念"))
    conn.commit()
    conn.close()

    pipe = FakePipeline()
    run_backfill(db_path=path, pipe=pipe, skip_existing=True, sleep_s=0)
    out = capsys.readouterr().out
    assert "skip=1" in out and "redo=2" in out


# ------------------------------------------------------------ 探针 CSV 兼容

def test_load_available_dates_reads_csv(tmp_path, monkeypatch):
    """load_available_dates 兼容现状 rank 探针 CSV 格式与 holes 逻辑."""
    csv = tmp_path / "probe.csv"
    csv.write_text(
        "date,rank15,rank17\n2025-01-02,398,235\n2025-01-03,0,0\n2026-05-01,10,0\n")
    monkeypatch.setattr("scripts.backfill_concept_oldtail.PROBE_CSV", str(csv))
    avail, holes = load_available_dates()
    assert "2025-01-02" in avail
    assert "2025-01-03" in holes
    assert "2026-05-01" not in avail and "2026-05-01" not in holes  # 段外

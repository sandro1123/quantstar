"""前置卡（D 审查产物）两改进项 — 08-30 统筹派单

① 漂移安全网: load_completed_days 联查 concept_daily COUNT —
   标记天零行 = 降级待跑 + WARNING (一条 SQL, 不动跳过行为本身)
② 日志统计可见性: 统计 print 全被 nohup 块缓冲吞 (D 实测 resume 日志 740 行
   全 stderr, 统计 0 条落盘) — print→flush=True (铁律 #12 可观测不可静默)

D 审查背景: 9757555 总评 GO 合并; 两改进是重挂 cron 前置 (非合并前置)。
"""
import inspect
import sqlite3

import scripts.backfill_concept_oldtail as mod
from scripts.backfill_concept_oldtail import (
    load_completed_days,
    mark_day_done,
    run_backfill,
)


class FakePipeline:
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


# ------------------------------------------------- ① 漂移安全网

def test_marked_day_with_zero_rows_degrades_to_rerun(tmp_path, monkeypatch, capsys):
    """安全网: 标记天但 concept_daily 该日零行 → 不跳过, 重跑 + WARNING 打印."""
    dates = [f"2025-01-0{i}" for i in range(1, 4)]
    monkeypatch.setattr(
        mod, "PROBE_CSV", _mk_dates_csv(tmp_path, dates))
    conn, path = _mk_conn(tmp_path)
    # d1: 标记 + 有行 (完整)
    mark_day_done(conn, dates[0])
    conn.execute(
        "INSERT INTO concept_daily VALUES ('000001',?,?,1.0,'x')", (dates[0], "概念"))
    # d2: 标记但零行 — 漂移现场 (表被清/写入丢失)
    mark_day_done(conn, dates[1])
    conn.commit()
    conn.close()

    pipe = FakePipeline()
    stats = run_backfill(db_path=path, pipe=pipe, skip_existing=True, sleep_s=0)
    assert dates[1] in pipe.calls, "零行标记天必须降级重跑, 不得静默跳过"
    assert dates[0] not in pipe.calls, "完整天照常跳过"
    out = capsys.readouterr().out
    assert "WARNING" in out and dates[1] in out, "漂移须打 WARNING 且点名日期"
    assert stats["skip"] == 1 and stats["redo"] == 2


def test_marked_day_with_rows_still_skips(tmp_path, monkeypatch):
    """安全网不误伤: 标记天有行 → 照常跳过 (行为不变)."""
    dates = [f"2025-01-0{i}" for i in range(1, 4)]
    monkeypatch.setattr(
        mod, "PROBE_CSV", _mk_dates_csv(tmp_path, dates))
    conn, path = _mk_conn(tmp_path)
    for d in dates[:2]:
        mark_day_done(conn, d)
        conn.execute(
            "INSERT INTO concept_daily VALUES ('000001',?,?,1.0,'x')", (d, "概念"))
    conn.commit()
    conn.close()

    pipe = FakePipeline()
    stats = run_backfill(db_path=path, pipe=pipe, skip_existing=True, sleep_s=0)
    assert sorted(pipe.calls) == [dates[2]]
    assert stats["skip"] == 2 and stats["redo"] == 1


# ------------------------------------------------- ② 统计 flush

def test_stats_prints_have_flush(tmp_path, monkeypatch):
    """统计 print 必须 flush=True — nohup 块缓冲下不 flush 就永远不落盘 (D 实测)."""
    import ast
    for fname in ("run_backfill", "main", "drift_guard"):
        tree = ast.parse(inspect.getsource(getattr(mod, fname)))
        prints = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and getattr(n.func, "id", None) == "print"]
        assert prints, f"{fname} 里找不到 print — 测试失效需修"
        for i, call in enumerate(prints):
            kw_names = [kw.arg for kw in call.keywords]
            assert "flush" in kw_names, (
                f"{fname} 第 {i} 个 print 缺 flush=True")

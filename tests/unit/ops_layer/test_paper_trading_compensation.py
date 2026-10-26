"""W2: paper_trading 补偿腿 — DDP/factor 断链日的当日补跑 (照 fx_settlement 先例)。

病根 (2026-09-19 审计实证):
  09-10/09-11 两天 daily_data_pipeline failed → factor_update 16:15 skipped →
  paper_trading 16:35 **skipped**(depends_on: [factor_update]) → 当日 PTS 行
  total_signals=None (真断链, 非记录缺陷)。
  factor_update 有 18:05 fx_settlement 补跑腿; **paper_trading 无任何补偿腿**
  → 断链日永久丢失当日信号消费。

修法: 顶层 evening_paper_trading 补偿腿 (depends_on: []), 幂等守卫:
  当日 PTS 行已有 total_signals>0 → 短路 (正常日零开销);
  缺失/None → 补跑 _run_paper_trading。
"""
import sqlite3

import pytest

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


def _orch():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_trading_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
        total_signals INTEGER, filled_signals INTEGER, avg_slippage REAL,
        max_slippage REAL, paper_return REAL, benchmark_return REAL,
        created_at TEXT DEFAULT (datetime('now','+8 hours')))""")
    conn.commit()
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    return db, orch


def _seed_pts(db, date, total_signals):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO paper_trading_stats (date, total_signals, filled_signals) VALUES (?,?,?)",
        (date, total_signals, total_signals))
    conn.commit()


def test_compensation_skips_when_pts_row_has_signals(monkeypatch):
    """正常日: 当日 PTS 已有信号 → 短路, 不重复跑 (零开销)。"""
    db, orch = _orch()
    _seed_pts(db, "2026-09-18", 22)
    calls = []
    monkeypatch.setattr(orch, "_run_paper_trading",
                        lambda cfg: calls.append(cfg) or _ok())
    r = orch._run_evening_paper_trading({"date": "2026-09-18"})
    assert r.success and r.metadata.get("skipped") is True
    assert calls == [], "正常日不得重复消费"


def test_compensation_runs_when_row_missing_signals(monkeypatch):
    """断链日: PTS 行 total_signals 为 NULL → 补跑 (W2 核心修复)。"""
    db, orch = _orch()
    _seed_pts(db, "2026-09-10", None)
    calls = []
    monkeypatch.setattr(orch, "_run_paper_trading",
                        lambda cfg: calls.append(cfg) or _ok())
    r = orch._run_evening_paper_trading({"date": "2026-09-10"})
    assert r.success and r.metadata.get("repaired") is True
    assert len(calls) == 1, "断链日必须补跑一次"


def test_compensation_runs_when_row_absent(monkeypatch):
    """完全缺行日 (16:35 从未跑成) → 同样补跑。"""
    db, orch = _orch()
    calls = []
    monkeypatch.setattr(orch, "_run_paper_trading",
                        lambda cfg: calls.append(cfg) or _ok())
    r = orch._run_evening_paper_trading({"date": "2026-09-11"})
    assert r.success and r.metadata.get("repaired") is True
    assert len(calls) == 1


def test_compensation_registered_in_dispatch():
    """注册面: 补偿腿须进 dispatch, 且 ops.yaml 有声明 (防线必须有载体)。"""
    import inspect
    src = inspect.getsource(PipelineOrchestrator._execute_pipeline)
    code = [l for l in src.splitlines() if not l.strip().startswith("#")]
    assert any("evening_paper_trading" in l for l in code), "dispatch 未注册补偿腿"


def test_ops_yaml_declares_compensation_no_deps():
    """配置面: 补偿腿 depends_on 必须为空 (否则 DDP 挂时又被 skip, 等于没修)。"""
    import yaml
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    cfg = yaml.safe_load((root / "config/defaults/ops.yaml").read_text(encoding="utf-8"))
    p = cfg["ops"]["pipelines"].get("evening_paper_trading")
    assert p is not None, "ops.yaml 未声明 evening_paper_trading"
    assert p.get("depends_on") == [], "补偿腿不得有依赖 (断链日仍须可跑)"
    assert p.get("schedule"), "须有排程时刻"


def _ok():
    from ops_layer.orchestrator import HandlerResult
    return HandlerResult(success=True, metadata={"date": "x", "status": "completed"})


def test_multirow_null_newest_does_not_retrigger():
    """同日多行 (partial 后补跑): 新est NULL + 旧行有值 → 不得重复消费。

    守卫语义 = MAX (任一行有值即已消费)。PTS 是纯 INSERT (无 PK/REPLACE),
    LIMIT 1 拿最新 NULL 会误判未消费 → double-consume (独立复核抓出)。
    """
    db, orch = _orch()
    _seed_pts(db, "2026-09-18", 22)
    _seed_pts(db, "2026-09-18", None)  # 后写 NULL 行 (partial 重跑的形态)
    calls = []
    import unittest.mock as m
    orig = orch._run_paper_trading
    with m.patch.object(orch, "_run_paper_trading",
                        side_effect=lambda cfg: calls.append(1) or orig(cfg)):
        r = orch._run_evening_paper_trading({"date": "2026-09-18"})
    assert r.success and r.metadata.get("skipped") is True, r.metadata
    assert calls == [], "同日已有消费记录时 NULL 新行不得触发重复消费"

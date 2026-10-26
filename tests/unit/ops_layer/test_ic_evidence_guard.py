"""Q4: IC 质量闸门守卫 — 近30日真IC证据<5条时 ic_decay no-op 不稀释。

ST-30 教训: 证据不足时的 decay 稀释 = 噪声写库 (88 行脏权重事故)。
守卫语义: record_ic 照常积累证据; 有现权重但证据 <5 → 保持现权重不跑
decay, warning 留痕走 degraded; 无权重时 default_seed 不受影响 (初始
播种不依赖 IC 证据)。
"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


def _mk_orch():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute(
        "CREATE TABLE factor_daily (date TEXT, code TEXT, factor_scores TEXT, "
        "fwd_return_1d REAL, fwd_return_5d REAL, fwd_return_10d REAL, "
        "fwd_return_20d REAL, PRIMARY KEY (date, code))")
    conn.execute(
        "CREATE TABLE factor_weights (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "weight_date TEXT NOT NULL, factor_name TEXT NOT NULL, "
        "weight REAL NOT NULL, method TEXT, old_weight REAL, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(weight_date, factor_name))")
    conn.execute(
        "CREATE TABLE factor_ic (calc_date TEXT NOT NULL, "
        "factor_name TEXT NOT NULL, mean_ic REAL, std_ic REAL, icir REAL, "
        "win_rate REAL, effectiveness TEXT, sample_size INTEGER, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (calc_date, factor_name))")
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    return db, orch


def _seed_ic_rows(db, date, fwd_col):
    conn = db.get_connection()
    for i in range(12):
        code = "%06d" % i
        conn.execute(
            f"INSERT INTO factor_daily (date, code, factor_scores, {fwd_col}) "
            "VALUES (?, ?, ?, ?)",
            (date, code, json.dumps({"roe_quality": float(i)}), float(i) * 0.1))
    conn.commit()


def _seed_ic_rows_span(db, fwd_col, n_days=15):
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    for k in range(n_days):
        _seed_ic_rows(db, (today - _td(days=k)).strftime("%Y-%m-%d"), fwd_col)


def _seed_factor_ic_history(db, n_rows):
    """向 factor_ic 直插 n_rows 条历史 IC 证据 (模拟既往管线积累)。"""
    from datetime import date as _d, timedelta as _td
    conn = db.get_connection()
    today = _d.today()
    for k in range(n_rows):
        d = (today - _td(days=k + 1)).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO factor_ic (calc_date, factor_name, mean_ic, std_ic, "
            "icir, win_rate, effectiveness, sample_size, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (d, "roe_quality", 0.05, 0.02, 0.4, 0.6, "strong", 12, d))
    conn.commit()


def _seed_weights(db):
    """预置现权重 — 全部 DEFAULT_WEIGHTS 因子等权, 让 ST-31 专家权重
    探针 (sum>=0.9) 通过, 隔离出本守卫的行为。"""
    from strategy_layer.aggregation.aggregator import DEFAULT_WEIGHTS
    conn = db.get_connection()
    total = sum(DEFAULT_WEIGHTS.values())
    for fname, w in DEFAULT_WEIGHTS.items():
        conn.execute(
            "INSERT INTO factor_weights (weight_date, factor_name, weight, "
            "method) VALUES (?,?,?,?)",
            ("2026-08-20", fname, w / total, "ic_decay"))
    conn.commit()


def _count_weight_rows(db):
    return db.get_connection().execute(
        "SELECT COUNT(*) FROM factor_weights").fetchone()[0]


def test_guard_noop_when_evidence_below_5():
    """有现权重 + 近30日证据 3 条历史 + 1 条当日 = 4 < 5 → decay no-op。

    断言: 权重表行数不变 (不写新权重行) + ic_evidence_guard=True 留痕。
    语义: 保护性 no-op (伤害被预防), 不翻 status — 故 status 仍 completed。
    """
    db, orch = _mk_orch()
    _seed_ic_rows_span(db, "fwd_return_10d")
    _seed_weights(db)
    _seed_factor_ic_history(db, 3)   # 3 历史 + 1 当日 = 4 < 5
    before = _count_weight_rows(db)
    result = orch._run_ic_feedback({"strategy": {"ic_horizon": 10}})
    assert result.success is True
    assert result.metadata["ic_evidence_guard"] is True, result.metadata
    assert result.metadata["ic_evidence_count"] == 4
    assert result.metadata["n_weights_updated"] == 0
    assert _count_weight_rows(db) == before   # 未写新权重行
    db.close()


def test_guard_passes_when_evidence_ge_5():
    """5 条历史 + 1 条当日 = 6 ≥ 5 → 正常走 decay 更新。"""
    db, orch = _mk_orch()
    _seed_ic_rows_span(db, "fwd_return_10d")
    _seed_weights(db)
    _seed_factor_ic_history(db, 5)   # 5 历史 + 1 当日 = 6 >= 5
    result = orch._run_ic_feedback({"strategy": {"ic_horizon": 10}})
    assert result.success is True
    assert result.metadata["status"] == "completed", result.metadata
    db.close()


def test_guard_does_not_block_default_seed():
    """无现权重时 default_seed 不受守卫影响 (初始播种不依赖 IC 证据)。"""
    db, orch = _mk_orch()
    _seed_ic_rows_span(db, "fwd_return_10d")
    # 不预置权重, 不铺历史 IC → 证据仅当日 1 条 < 5, 但应照常 seed
    result = orch._run_ic_feedback({"strategy": {"ic_horizon": 10}})
    assert result.success is True
    assert result.metadata["status"] == "completed", result.metadata
    assert _count_weight_rows(db) >= 1   # DEFAULT_WEIGHTS 已播种
    db.close()

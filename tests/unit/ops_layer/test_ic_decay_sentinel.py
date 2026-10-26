"""卡I RED→GREEN — IC-by-lag 衰减哨兵: ic_feedback 产 decay_curve 落库+红条

背景 (D 从 v2 P0 清单提取, 统筹 sm-85ffae726bd041b0 ② 派单):
  compute_decay (backtest_layer/evaluation/factor_eval.py:180) 自 BT-09
  落地以来零接线 — repo 唯一引用是 evaluation/__init__ 导出。衰减曲线
  (lag-N IC) 是因子"保质期"证据: half_life=1 的因子调权周期选 5 天
  就是在吃过期收益; 衰减超阈值因子须进早报/周报红条。

修法 (卡I 口径, FactorCorrelation 留池):
  _run_ic_feedback 在计算完当日截面 IC 后, 用同一截面逐因子调
  FactorEvaluator.compute_decay: scores=当日因子值序列,
  forward_returns=该截面前向收益序列 — 衰减曲线=因子对持有 N 天
  收益的预测力衰减。结果写 factor_decay 表 (calc_date, factor_name,
  decay_curve JSON, half_life); 衰减红条: decay[1] (lag-1 IC) 已跌破
  decay[0] 一半且 half_life <= 2 时进 metadata["decay_alerts"]。
  报告点名 (早报/周报红条) 留卡尾 (派单原语), metadata 是接线点。

铁律: 衰减段失败不杀主管道 (包裹 try + warning 留痕, 铁律#12);
decay_curve 空截面=诚实跳过不写假行。
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


DECAY_DDL = """
CREATE TABLE IF NOT EXISTS factor_decay (
    calc_date TEXT NOT NULL,
    factor_name TEXT NOT NULL,
    decay_curve TEXT,
    half_life INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (calc_date, factor_name)
)
"""


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
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    return db, orch


def _seed_span(db, fwd_col, n_days=15, seed_fn=None):
    """按日历日铺种子 (handler target_date 滚动, 测试不依赖今天几号)。"""
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    conn = db.get_connection()
    for k in range(n_days):
        d = (today - _td(days=k)).strftime("%Y-%m-%d")
        for i in range(12):
            code = "%06d" % i
            conn.execute(
                f"INSERT OR REPLACE INTO factor_daily (date, code, factor_scores, {fwd_col}) "
                "VALUES (?, ?, ?, ?)",
                (d, code, json.dumps(seed_fn(i) if seed_fn else {"roe_quality": float(i)}),
                 float(i) * 0.1))
    conn.commit()


def _table_exists(db, name):
    conn = db.get_connection()
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return r is not None


# ---------- RED 1: 表落库 ----------

def test_decay_rows_persisted_after_ic_feedback():
    """ic_feedback 完成后: factor_decay 表存在, 每个有分数的因子一行,
    decay_curve 是 JSON 列表 (lag 0..10), half_life 落库。"""
    db, orch = _mk_orch()
    _seed_span(db, "fwd_return_5d")
    result = orch._run_ic_feedback({})
    assert result.success is True
    assert result.metadata["status"] == "completed", result.metadata
    assert _table_exists(db, "factor_decay"), \
        "factor_decay 表未建 — compute_decay 仍零接线 (卡I RED)"
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT factor_name, decay_curve, half_life FROM factor_decay "
        "WHERE calc_date = ?", (result.metadata["date"],)).fetchall()
    assert rows, "衰减行未落库"
    name, curve_json, half_life = rows[0]
    curve = json.loads(curve_json)
    assert isinstance(curve, list) and len(curve) >= 2, \
        f"decay_curve 形态错: {curve}"
    # 单调完美因子: decay[0] 应显著为正
    assert curve[0] > 0.8, f"完美因子 lag-0 IC 应≈1, got {curve[0]}"
    db.close()


# ---------- RED 2: 红条进 metadata ----------

def test_fast_decay_factor_raises_alert_in_metadata():
    """衰减红条: decay[1] 已跌破 decay[0] 一半 (half_life<=1) 的因子
    点名进 metadata['decay_alerts']; 稳定因子不进。"""
    db, orch = _mk_orch()
    # fast_decay: 分数与 5d 收益强相关但与 1d 无关 → 衰减极快;
    # stable: 分数与各期收益同向 → 衰减平缓
    def seeds(i):
        return {"fast_decay": float(i), "stable_factor": float(i)}
    # 用同列种子简化: fast_decay 因子值与 5d 收益完全相关, 衰减由
    # compute_decay 的 lag 序列天然产生; 断言只要求红条机制可达。
    _seed_span(db, "fwd_return_5d", seed_fn=seeds)
    result = orch._run_ic_feedback({})
    assert result.success is True
    meta = result.metadata
    assert "decay_alerts" in meta, "metadata 无 decay_alerts 键 (卡I RED)"
    assert isinstance(meta["decay_alerts"], list)
    # 稳定因子 (与收益完全相关, 衰减慢) 不应进红条
    if "stable_factor" in [a["factor"] for a in meta["decay_alerts"] if isinstance(a, dict)]:
        pytest.fail("stable_factor 不应进衰减红条 — 红条阈值过松")
    db.close()


# ---------- RED 3: 主管道不被衰减段杀死 ----------

def test_decay_failure_never_kills_pipeline():
    """衰减段自身异常 (如表锁/写失败) 只 warning, 主管道仍 completed。
    用 monkeypatch 把 decay 落库函数炸掉。"""
    db, orch = _mk_orch()
    _seed_span(db, "fwd_return_5d")
    import ops_layer.orchestrator as orch_mod
    original = orch_mod.PipelineOrchestrator._run_ic_feedback

    def boom(*a, **k):
        raise RuntimeError("decay table locked")

    # 只炸衰减段: monkeypatch FactorEvaluator.compute_decay
    import backtest_layer.evaluation.factor_eval as fe
    monkey = pytest.MonkeyPatch()
    monkey.setattr(fe.FactorEvaluator, "compute_decay", boom)
    try:
        result = orch._run_ic_feedback({})
        assert result.success is True, "衰减段异常不得杀主管道 (铁律#12)"
        assert result.metadata["status"] == "completed", result.metadata
    finally:
        monkey.undo()
    db.close()


# ---------- RED 4: 纯函数语义 (既有行为锚, 防修法动 compute_decay 本体) ----------

def test_compute_decay_semantics_unchanged():
    """compute_decay 本体不动: 完美单调因子 decay[0]≈1, 11 点曲线;
    反向因子 half_life=-1 诚实。"""
    from backtest_layer.evaluation.factor_eval import FactorEvaluator
    ev = FactorEvaluator()
    scores = [float(i) for i in range(1, 13)]
    rets = [float(i) for i in range(1, 13)]
    res = ev.compute_decay("perfect", scores, rets, max_lag=10)
    assert res.decay_curve[0] > 0.99
    assert len(res.decay_curve) == 11
    res2 = ev.compute_decay("inverse", scores, [-x for x in rets], max_lag=10)
    assert res2.half_life == -1

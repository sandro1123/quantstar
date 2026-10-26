"""#57 RED — 因子分布漂移哨兵 (b路线: sklearn 原生四件, 零新增依赖)

背景 (统筹 sm-832ad940cd864f31 裁 b 路线; 卡#57 立项要点):
  factor_daily.factor_scores 每日 88 因子 x ~5547 股截面, 因子分布
  漂移 (regime 变化/数据质量退化/成分股变迁) 现零检测 — 0814 因子
  退化事件靠人发现。哨兵域要的是"检测+告警", sklearn 原生四件
  (PSI/KS/卡方/Wasserstein) 完全覆盖, Evidently 增值在可视化=
  仪表盘域不在本卡。

设计 (与卡I factor_decay 同域互补: decay=时滞衰减, drift=分布漂移):
  - 新模块 backtest_layer/evaluation/factor_drift.py:
    compute_drift(reference, current) → FactorDriftResult
    四件指标: psi (10桶分箱, >0.25 显著漂移), ks_pvalue (<0.01
    显著), chi2_pvalue (<0.01), wasserstein (归一化到 reference
    std, >1.0 显著); triggered = PSI OR KS OR CHI2 OR WASSERSTEIN
    (任一触发即证据 — 聚合语义统筹已授权按卡I单阈值口径, 不加权:
    四件量纲不同, 加权需先归一化=引入自由参数, 哨兵域不值当)。
  - 挂点: _run_ic_feedback 段内紧随 decay 段 (同一截面复用,
    reference=滚动 T-N 因子自身分布, current=当日) — 表
    factor_drift_metrics (calc_date, factor_name, psi, ks_pvalue,
    chi2_pvalue, wasserstein, triggered) 管道内 CREATE IF NOT
    EXISTS (DL-43 教训), 漂移因子进 metadata["drift_alerts"]
    + warning (铁律#12); 段内失败不杀主管道。
  - 开关: config factor_drift.enabled 默认 False — 零依赖零接线
    通电前不改变现产行为; 通电=纯配置翻转 (signal_provenance 先例)。

本 RED 文件: 纯函数语义 (compute_drift) + 管道接线 (表落库/
metadata 红条/段隔离/开关门控) 四测, 先红后绿。
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from common.database import Database


# ---------- RED 1: 模块存在 + 纯函数语义 ----------

def test_compute_drift_module_and_semantics():
    """compute_drift 四件指标 + triggered=OR 聚合:
    同分布 → 全不触发; 平移1σ → PSI/Wasserstein 触发。"""
    from backtest_layer.evaluation.factor_drift import compute_drift
    import numpy as np

    rng = np.random.default_rng(42)
    ref = rng.normal(0.0, 1.0, 500).tolist()
    same = rng.normal(0.0, 1.0, 500).tolist()      # 同分布
    shifted = rng.normal(2.0, 1.0, 500).tolist()   # 平移2σ

    ok = compute_drift("f_stable", ref, same)
    assert ok.triggered is False, f"同分布不应触发: psi={ok.psi}"
    assert 0.0 <= ok.psi <= 0.25
    assert 0.0 <= ok.ks_pvalue <= 1.0

    drift = compute_drift("f_shifted", ref, shifted)
    assert drift.triggered is True, "平移2σ必须触发"
    assert drift.psi > 0.25
    assert drift.wasserstein_norm > 1.0


def test_compute_drift_honesty_on_empty():
    """空截面诚实跳过: None/短序列 → triggered=None 不造假。"""
    from backtest_layer.evaluation.factor_drift import compute_drift
    empty = compute_drift("f_empty", [0.1, 0.2], [0.3, 0.4])
    assert empty is not None and empty.triggered is None, \
        "样本不足须返回 triggered=None (不可评估), 不冒充不漂移"


# ---------- RED 2/3/4: 管道接线 (开关门控 + 表落库 + 段隔离) ----------

def _mk_orch_with_drift(enabled):
    """cfg 走 _run_ic_feedback 入参 (c6a602b 双形态契约,
    同 test_ic_feedback_horizon 直接传扁平 dict)。"""
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute(
        "CREATE TABLE factor_daily (date TEXT, code TEXT, factor_scores TEXT, "
        "fwd_return_5d REAL, PRIMARY KEY (date, code))")
    from ops_layer.orchestrator import PipelineOrchestrator
    from ops_layer.pipeline_logger import PipelineLogger
    from ops_layer.alert_manager import AlertManager
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    cfg = {"factor_drift.enabled": enabled,
           "factor_drift.reference_window": 10}
    return db, orch, cfg


def _seed(db, n_days=15):
    """铺 n 天: 全程稳定分布, 唯 target_date (ic_feedback 实际评估
    的滞后截面, horizon 默认 5 → 今天回数 5 个交易日, ST-31 跳周末)
    size_factor 平移漂移; fwd_return_5d 有值让 ic_feedback 走到
    decay/drift 段。

    漂移日必须自对齐 _resolve_ic_target_date 而非硬编码 k: 探针
    实证 today=周一 时 target=自然日 k=7 (周末被交易日历跳过),
    k=0/k=5 都放错位 — 漂移日不在评估位时 triggered=0 是正确
    行为, 断言红是种子错 (RED 实证: k=0 时 size_factor=1 其实
    是 PSI 假阳, 分箱守卫落地后归零暴露)。"""
    from datetime import date as _d, timedelta as _td
    import numpy as np
    from ops_layer.orchestrator import _resolve_ic_target_date
    rng = np.random.default_rng(7)
    conn = db.get_connection()
    today = _d.today()
    drift_date = _resolve_ic_target_date(db, 5)  # horizon 默认 5
    for k in range(n_days):
        d = (today - _td(days=k)).strftime("%Y-%m-%d")
        for i in range(30):
            base = rng.normal(0.0, 1.0)
            drift_shift = 3.0 if d == drift_date else 0.0
            scores = {"size_factor": base + drift_shift,
                      "stable_factor": base}
            conn.execute(
                "INSERT OR REPLACE INTO factor_daily (date, code, factor_scores, fwd_return_5d) "
                "VALUES (?, ?, ?, ?)",
                (d, "%06d" % i, json.dumps(scores), float(i) * 0.01))
    conn.commit()


def _table_exists(db, name):
    conn = db.get_connection()
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return r is not None


def test_drift_disabled_by_default_no_table_no_write():
    """开关默认关 (enabled=False): 不建表不写行 — 零现产行为改变。"""
    db, orch, cfg = _mk_orch_with_drift(enabled=False)
    _seed(db)
    result = orch._run_ic_feedback(cfg)
    assert result.success is True
    assert _table_exists(db, "factor_drift_metrics") is False, \
        "开关关闭不得建表/写行 (默认关铁律, #57 RED)"
    # metadata 恒带 drift_alerts 键 (空列表=诚实"已评估未发现"),
    # 开关关的零改变断言=红条恒空。
    assert result.metadata.get("drift_alerts") == [], \
        "开关关闭红条必须为空: %r" % result.metadata.get("drift_alerts")


def test_drift_enabled_persists_rows_and_alerts():
    """开关开: factor_drift_metrics 落库 + 漂移因子进 metadata
    ['drift_alerts'] + 稳定因子不进 (对偶断言)。"""
    db, orch, cfg = _mk_orch_with_drift(enabled=True)
    _seed(db)
    result = orch._run_ic_feedback(cfg)
    assert result.success is True, result.metadata
    assert _table_exists(db, "factor_drift_metrics"), \
        "factor_drift_metrics 未建 — compute_drift 仍零接线 (#57 RED)"
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT factor_name, triggered FROM factor_drift_metrics "
        "WHERE calc_date = ?", (result.metadata["date"],)).fetchall()
    names = {r["factor_name"] for r in rows}
    assert "size_factor" in names and "stable_factor" in names, rows
    trig = {r["factor_name"]: r["triggered"] for r in rows}
    assert trig.get("size_factor") == 1, f"平移漂移因子必须 triggered=1: {trig}"
    assert trig.get("stable_factor") == 0, f"稳定因子不得误报: {trig}"
    alerts = result.metadata.get("drift_alerts")
    assert isinstance(alerts, list) and alerts, "漂移红条未进 metadata"
    alert_factors = {a["factor"] for a in alerts if isinstance(a, dict)}
    assert "size_factor" in alert_factors, alerts
    assert "stable_factor" not in alert_factors, \
        f"稳定因子不得进红条 (误报): {alerts}"


def test_binned_detectors_honest_none_on_small_current():
    """分箱族小样本诚实跳过: current=30 (10桶x3/桶, 空桶结构性
    假阳 70.7% H0 实测) → psi=None+chi2=None; KS/W 非分箱族
    (30>=_MIN_SAMPLES) 继续判; 同分布 triggered=False 不误报。"""
    from backtest_layer.evaluation.factor_drift import compute_drift
    import numpy as np

    rng = np.random.default_rng(99)
    ref = rng.normal(0.0, 1.0, 300).tolist()   # 10日x30股镜像
    same = rng.normal(0.0, 1.0, 30).tolist()
    ok = compute_drift("f_small", ref, same)
    assert ok.psi is None, \
        f"current=30 分箱族必须诚实跳过 psi=None (H0假阳70.7%实测): {ok.psi}"
    assert ok.chi2_pvalue is None, \
        f"current=30 卡方 expected=3<5 须跳过: {ok.chi2_pvalue}"
    assert ok.ks_pvalue is not None, "KS 非分箱族 30>=_MIN_SAMPLES 须照判"
    assert ok.wasserstein_norm is not None, "W 非分箱族 30>=_MIN_SAMPLES 须照判"
    assert ok.triggered is False, f"同分布小样本不得误报: {ok}"


def test_binned_detectors_small_current_drift_still_caught():
    """对偶: current=30 但 3σ 平移 — 分箱族跳过, KS/W 仍须抓到
    (哨兵小样本日不瞎: 证据来自非分箱族)。"""
    from backtest_layer.evaluation.factor_drift import compute_drift
    import numpy as np

    rng = np.random.default_rng(100)
    ref = rng.normal(0.0, 1.0, 300).tolist()
    shifted = rng.normal(3.0, 1.0, 30).tolist()
    r = compute_drift("f_small_shift", ref, shifted)
    assert r.psi is None, "current=30 分箱族跳过 (漂移日同样跳)"
    assert r.triggered is True, \
        f"3σ平移 KS/W 必须触发 (分箱族跳过不等于放行): {r}"


def test_drift_failure_never_kills_pipeline():
    """段内异常只 warning, 主管道仍 completed (铁律#12)。"""
    db, orch, cfg = _mk_orch_with_drift(enabled=True)
    _seed(db)
    import backtest_layer.evaluation.factor_drift as fd
    monkey = pytest.MonkeyPatch()
    def boom(*a, **k):
        raise RuntimeError("drift table locked")
    monkey.setattr(fd, "compute_drift", boom)
    try:
        result = orch._run_ic_feedback(cfg)
        assert result.success is True, "漂移段异常不得杀主管道 (铁律#12)"
        assert result.metadata["status"] == "completed", result.metadata
    finally:
        monkey.undo()

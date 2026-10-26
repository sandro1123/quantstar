# -*- coding: utf-8 -*-
"""#108 策略层门键接线 — RED 套件 (案A: 读法改 Config 全键空间属主)。

病理 (D-293 取证 + sm-08e435ad 裁定):
    handler 的 cfg 是管道块作用域 — run_pipeline 里
    `get_section("ops.pipelines")` 把 yaml 扁平成点键后又重建为嵌套 dict,
    所以 wfo_gate/param_tuning 块只含调度键。flat 点键
    "backtest.wfo_auto_deploy.enabled" 从管道块恒 MISS → 门恒回落默认 False
    (开关是死的)。local.yaml 的键在 registry 顶层可达, 但 handler 从不读
    Config — 生产构造点 (ops_layer/__init__.py:55) 明明传了 config。

修法 (案A, 对齐 _dep_data_probe_enabled 先例):
    ① orchestrator 增 `_cfg_all(dotted, default, cfg)` — Config-first
       (self.config.get), cfg 块双形态兜底 (flat→嵌套, 保既有直传测试
       契约), 最后 default;
    ② wfo_gate/_apply_wfo_tier/param_tuning/ic_feedback 族读点全部改走
       _cfg_all;
    ③ ops.yaml ic_feedback 块内 strategy 嵌套撤除 (生产值迁 local.yaml
       strategy.ic_horizon: 10);
    ④ read_wfo_paper_weights 读腿挂真实消费者: PaperTradingRunner
       wfo_weights_on_test() 摘要 + _run_paper_trading meta 挂载
       (纯可观测, 不动打分/执行)。

RED 语义: 每个非守卫测试当前必须红, 且红因 = read-MISS (Config 键在场
但 handler 读不到) 或特性缺失 (_cfg_all / wfo_weights_on_test 不存在),
不是夹具/笔误。守卫组两测当前绿、GREEN 后仍须绿 (防改造破坏健康路径)。
"""
import inspect
import json
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from common.database import Database
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger


# ─── 夹具 ─────────────────────────────────────────────────────────

class _Cfg:
    """dict-backed Config 替身 (对齐 test_dep_data_probe._Cfg 先例),
    扮演 Config 全键空间属主角色。"""

    def __init__(self, values: dict | None = None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


def _mk_db():
    db = Database(":memory:")
    db.init_db()
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db


def _mk_orch(config=None):
    db = _mk_db()
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db), config)
    return db, orch


def _seed_factor_weights(db, n=3):
    """deploy/paper 档的权重源 (表不存在时首种)。n=0 → 只建空表。"""
    from common.utils import bj_date
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS factor_weights ("
                 "weight_date TEXT, factor_name TEXT, weight REAL)")
    for i in range(n):
        conn.execute(
            "INSERT INTO factor_weights (weight_date, factor_name, weight) "
            "VALUES (?, ?, ?)", (bj_date(), f"expert_{i}", 0.3 - 0.1 * i))
    conn.commit()


def _wfo_pipeline_block():
    """生产形态 wfo_gate 管道块 — get_section 重建后只含调度键,
    门键不存在 (read-MISS 病发现场)。"""
    return {"schedule": "20.00", "days": "0", "depends_on": [],
            "timeout_s": 600, "max_retries": 2, "retry_delay_s": 60,
            "description": "WFO 门禁验证"}


# ─── RED-1: WFO deploy 门 — Config 有键 → 必须真部署 ───────────────

def test_wfo_deploy_gate_reads_config_space():
    """Config 持 backtest.wfo_auto_deploy.enabled=True + cfg=纯管道块 →
    "deployed" + active_factor_weights 落行。当前病: flat 键从管道块
    MISS → "deploy_record_only"。"""
    db, orch = _mk_orch(_Cfg({"backtest.wfo_auto_deploy.enabled": True}))
    _seed_factor_weights(db, n=3)
    action = orch._apply_wfo_tier("deploy", "2026-09-06", _wfo_pipeline_block())
    assert action == "deployed", (
        f"门键在 Config 但 handler 只读管道块 → read-MISS: action={action}")
    n = db.get_connection().execute(
        "SELECT COUNT(*) FROM active_factor_weights").fetchone()[0]
    assert n == 3, f"deployed 但只落 {n} 行 (期望 3) = 假绿"
    db.close()


# ─── RED-2: WFO paper 门 — Config 有键 → 必须落 paper_trading_weights ──

def test_wfo_paper_gate_reads_config_space():
    """Config 持 backtest.wfo_paper_persist.enabled=True → "paper_assigned"
    + paper_trading_weights 落行 (读腿消费端由此有真数据)。当前病:
    "paper_record_only"。"""
    db, orch = _mk_orch(_Cfg({"backtest.wfo_paper_persist.enabled": True}))
    _seed_factor_weights(db, n=3)
    action = orch._apply_wfo_tier("paper_trading", "2026-09-06",
                                 _wfo_pipeline_block())
    assert action == "paper_assigned", (
        f"门键在 Config 但 handler 只读管道块 → read-MISS: action={action}")
    n = db.get_connection().execute(
        "SELECT COUNT(*) FROM paper_trading_weights").fetchone()[0]
    assert n == 3, f"paper_assigned 但只落 {n} 行 (期望 3) = 假绿"
    db.close()


# ─── RED-3: #77 并案 — param_tuning persisted 门 ────────────────────

def test_param_tuning_persistence_gate_reads_config_space():
    """#77: Config 持 backtest.param_persistence.enabled=True (生产
    local.yaml 09-01 已落此键) + cfg=纯管道块 → persisted=True 且
    strategy_params 落行。当前病: flat 键从管道块 MISS → persisted:False
    (09-06 周日首跑必现病征 — 本卡硬期限)。"""
    db, orch = _mk_orch(_Cfg({"backtest.param_persistence.enabled": True}))
    conn = db.get_connection()
    conn.execute("CREATE TABLE post_exit_tracking ("
                 "code TEXT, exit_date TEXT, total_return_pct REAL)")
    conn.commit()
    block = {"schedule": "22.00", "days": "0", "depends_on": ["wfo_gate"],
             "timeout_s": 600, "max_retries": 2, "description": "参数调优"}
    result = orch._run_param_tuning(block)
    assert result.success is True
    assert result.metadata["persisted"] is True, (
        f"persisted:False = #77 病征 (门键在 Config 但 handler 只读管道块): "
        f"{result.metadata}")
    n = conn.execute("SELECT COUNT(*) FROM strategy_params").fetchone()[0]
    assert n >= 1, "persisted=True 但 strategy_params 0 行 = 假绿"
    db.close()


# ─── RED-4: wfo_lookback_days 读点 — Config 属主接线守卫 ─────────────

def test_wfo_lookback_days_read_point_uses_config():
    """wfo_lookback_days 读点改 Config-first。_run_wfo_gate 全链依赖
    bootstrap_app 不宜单测, 故接线守卫 (先例
    test_paper_runner.test_orchestrator_meta_includes_graduation):
    源码必须含 _cfg_all/self.config 读点, 不得只依赖 cfg.get 单形态。"""
    src_code = inspect.getsource(PipelineOrchestrator._run_wfo_gate)
    assert "wfo_lookback_days" in src_code
    assert ("_cfg_all" in src_code) or ("self.config" in src_code), (
        "wfo_lookback_days 读点仍是纯管道块读法 (Config 属主未接线)")


# ─── RED-5~7: ic_horizon 族 — Config 属主读法 ─────────────────────

def _mk_ic_orch(config=None):
    """ic 测试专用: OPS DDL + factor_daily 截面表 (对齐
    test_ic_horizon_nested._mk_orch 的表结构)。"""
    db = _mk_db()
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE factor_daily (date TEXT, code TEXT, factor_scores TEXT, "
        "fwd_return_1d REAL, fwd_return_5d REAL, fwd_return_10d REAL, "
        "fwd_return_20d REAL, PRIMARY KEY (date, code))")
    conn.commit()
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db), config)
    return db, orch


def _seed_ic_rows_span(db, fwd_col, n_days=20):
    """对齐 test_ic_horizon_nested 既有种法 (绿测验证过的口径):
    今天回铺 n_days 个日历日, 每日 12 只, 只有 fwd_col 有值。
    n=20 保证 lag 抬到 10 交易日时 target_date 仍在铺数窗内。"""
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    conn = db.get_connection()
    for k in range(n_days):
        d = (today - _td(days=k)).strftime("%Y-%m-%d")
        for i in range(12):
            conn.execute(
                f"INSERT OR REPLACE INTO factor_daily "
                f"(date, code, factor_scores, {fwd_col}) VALUES (?, ?, ?, ?)",
                (d, "%06d" % i, json.dumps({"roe_quality": float(i)}),
                 float(i) * 0.1))
    conn.commit()


def test_ic_horizon_config_space_reads_10d():
    """Config 持 strategy.ic_horizon=10 + cfg=纯管道块 (ic 同族一并清后
    的生产形态) → completed 走 10d。当前病: _cfg_get 只搜 cfg → 恒 5d
    → 只有 10d 数据时 skipped。"""
    db, orch = _mk_ic_orch(_Cfg({"strategy.ic_horizon": 10}))
    _seed_ic_rows_span(db, "fwd_return_10d")
    result = orch._run_ic_feedback({})
    assert result.metadata["status"] == "completed", result.metadata
    assert result.metadata["n_stocks"] == 12
    db.close()


def test_ic_lag_below_horizon_raised_under_config():
    """守卫不因读法改造失灵: Config ic_lag_days=3 < horizon=10 → 自动
    抬到 10 → completed。当前病: Config 被无视 → 恒 5d → skipped。"""
    db, orch = _mk_ic_orch(_Cfg({"strategy.ic_horizon": 10,
                                 "strategy.ic_lag_days": 3}))
    _seed_ic_rows_span(db, "fwd_return_10d")
    result = orch._run_ic_feedback({})
    assert result.metadata["status"] == "completed", result.metadata
    db.close()


def test_ic_config_precedence_over_pipeline_block():
    """优先级语义 (对齐 _dep_data_probe 先例): Config 有键 → Config 赢,
    即使 cfg 块里也有同键。Config=5 vs cfg 嵌套=10, 只有 10d 数据 →
    Config 赢 → 5d → skipped。当前病: Config 被无视 → 10d → completed。"""
    db, orch = _mk_ic_orch(_Cfg({"strategy.ic_horizon": 5}))
    _seed_ic_rows_span(db, "fwd_return_10d")
    result = orch._run_ic_feedback({"strategy": {"ic_horizon": 10}})
    assert result.metadata["status"] == "skipped", (
        f"Config(ic_horizon=5) 应优先于管道块(10): {result.metadata}")


# ─── RED-8~10: _cfg_all 助手行为 (方法尚不存在 → AttributeError 红) ──

def test_cfg_all_config_first():
    db, orch = _mk_orch(_Cfg({"backtest.wfo_auto_deploy.enabled": True}))
    v = orch._cfg_all("backtest.wfo_auto_deploy.enabled", False,
                      {"backtest.wfo_auto_deploy.enabled": False})
    assert v is True, "Config 有键时必须 Config 优先"


def test_cfg_all_cfg_fallback_flat_and_nested():
    """Config 无键 → cfg 块兜底: flat (测试直传契约) 与嵌套 (get_section
    生产形态) 双形态都要保 (既有 test_ic_horizon_nested 回归面)。"""
    db, orch = _mk_orch(None)
    assert orch._cfg_all("strategy.ic_horizon", 5,
                         {"strategy.ic_horizon": 10}) == 10
    assert orch._cfg_all("strategy.ic_horizon", 5,
                         {"strategy": {"ic_horizon": 10}}) == 10
    db.close()


def test_cfg_all_default_when_both_missing():
    db, orch = _mk_orch(_Cfg({}))
    assert orch._cfg_all("backtest.wfo_paper_persist.enabled", False, {}) is False
    db.close()


# ─── RED-11~13: read_wfo_paper_weights 读腿挂真实消费者 ─────────────

class _Db:
    """裸 sqlite 账本 (对齐 test_paper_runner._Db 先例)。"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _seed_paper_weights(db):
    conn = db.get_connection()
    conn.execute(
        "CREATE TABLE paper_trading_weights ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, assign_date TEXT NOT NULL, "
        "factor_name TEXT NOT NULL, weight REAL NOT NULL, "
        "source TEXT DEFAULT 'wfo_paper', "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(assign_date, factor_name))")
    for d, f, w in [("2026-09-01", "roe_quality", 0.5),
                    ("2026-09-01", "growth", 0.3),
                    ("2026-09-02", "roe_quality", 0.6),
                    ("2026-09-02", "growth", 0.4)]:
        conn.execute(
            "INSERT INTO paper_trading_weights (assign_date, factor_name, "
            "weight) VALUES (?, ?, ?)", (d, f, w))
    conn.commit()


def test_runner_wfo_weights_summary_reads_table():
    """读腿消费者: PaperTradingRunner.wfo_weights_on_test() 摘要
    paper_trading_weights (WFO 中间档权重在纸交易验证中的可观测面)。
    方法尚不存在 → AttributeError 红 (特性缺失)。"""
    from backtest_layer.paper_trading.runner import PaperTradingRunner
    db = _Db()
    _seed_paper_weights(db)
    runner = PaperTradingRunner(db=db, config=None)
    summary = runner.wfo_weights_on_test()
    assert summary is not None
    assert summary["n_weights"] == 4, summary
    assert summary["latest_assign_date"] == "2026-09-02", summary


def test_runner_wfo_weights_summary_empty_is_none():
    from backtest_layer.paper_trading.runner import PaperTradingRunner
    runner = PaperTradingRunner(db=_Db(), config=None)
    assert runner.wfo_weights_on_test() is None


def test_paper_trading_meta_mounts_wfo_weights():
    """挂载守卫: _run_paper_trading 源码必须消费 wfo_weights_on_test
    (meta 可观测, 铁律 #12) — 先例同 RED-4 接线守卫。"""
    src_code = inspect.getsource(PipelineOrchestrator._run_paper_trading)
    assert "wfo_weights_on_test" in src_code, (
        "read_wfo_paper_weights 读腿未挂入 paper_trading 管道 meta")


# ─── 守卫组 (当前绿、GREEN 后仍须绿 — 防改造破坏健康路径) ───────────

def test_wfo_deploy_no_weights_still_skips():
    """诚实跳过分支不因读法改造破坏: Config 有键但 factor_weights 空表
    → "deploy_skipped_no_weights" (不造假部署)。"""
    db, orch = _mk_orch(_Cfg({"backtest.wfo_auto_deploy.enabled": True}))
    _seed_factor_weights(db, n=0)  # 只建空表
    action = orch._apply_wfo_tier("deploy", "2026-09-06", {})
    assert action == "deploy_skipped_no_weights"
    db.close()


def test_param_tuning_no_data_still_success():
    """无表现数据 → 规则引擎 no_performance_data 维持基准; 门关 →
    persisted=False; success=True 不受损 (回归面)。"""
    db, orch = _mk_orch(_Cfg({}))
    conn = db.get_connection()
    conn.execute("CREATE TABLE post_exit_tracking ("
                 "code TEXT, exit_date TEXT, total_return_pct REAL)")
    conn.commit()
    result = orch._run_param_tuning({})
    assert result.success is True
    assert result.metadata["status"] == "completed"
    assert result.metadata["persisted"] is False
    db.close()

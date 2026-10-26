"""ST-31 验收测试 — IC 环可观测 (skip 可见 / 权重健康探针 / calc_date 真实日 / 交易日滞后)

病灶 (2026-08-24 派发, F-37/F-38):
1. _run_ic_feedback 每天 skip 并 return success=True → pipeline_runs 记 'success',
   IC 环停 8 天零告警 (静默成功);
2. factor_weights 里 20 个专家标定因子权重合计被 ic_decay 稀释到 46.7%, 无探针;
3. record_ic 内部 bj_date() 把 T-5 数据算出的 IC 错标成运行日 (calc_date 数据错标);
4. target_date 按自然日减, 周末/节假日必落空。

验收判据: 构造 skip 场景, 确认它留下「可查询的痕迹」(pipeline_runs.status='partial'
+ alerts 行), 而不是静默成功。负向对照: 健康/成功场景不得发 partial 告警。
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from common.database import Database
from execution_layer.migrations import ensure_schema
from ops_layer.alert_manager import AlertManager
from ops_layer.core.models import HandlerResult
from ops_layer.orchestrator import MIN_EXPERT_WEIGHT_SUM, PipelineOrchestrator, _resolve_ic_target_date
from ops_layer.pipeline_logger import PipelineLogger
from strategy_layer.aggregation.aggregator import DEFAULT_WEIGHTS


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

OPS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    pipeline_name TEXT NOT NULL,
    date TEXT NOT NULL,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    status TEXT NOT NULL,
    error_msg TEXT,
    duration_s REAL,
    version TEXT,
    config_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL,
    layer TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    action TEXT,
    status TEXT DEFAULT 'open',
    resolved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE factor_daily (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    factor_scores TEXT,
    fwd_return_5d REAL,
    PRIMARY KEY (date, code)
);
CREATE TABLE daily_klines (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    PRIMARY KEY (code, date)
);
CREATE TABLE factor_weights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weight_date TEXT NOT NULL,
    factor_name TEXT NOT NULL,
    weight REAL NOT NULL,
    method TEXT,
    old_weight REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(weight_date, factor_name)
);
CREATE TABLE factor_ic (
    calc_date TEXT NOT NULL,
    factor_name TEXT NOT NULL,
    mean_ic REAL,
    std_ic REAL,
    icir REAL,
    win_rate REAL,
    effectiveness TEXT,
    sample_size INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (calc_date, factor_name)
);
"""


@pytest.fixture
def env():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.executescript(OPS_DDL)
    conn.commit()
    # OPS_DDL 模拟迁移前的遗留 alerts 表 (无 last_seen) — 生产遗留库正是经
    # ensure_schema(v8) 升级后才与 AlertManager 的新 INSERT 契约对齐, 夹具同路。
    ensure_schema(db)
    orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
    # 只注册 ic_feedback 一个管道; handler 用真实现 (不 mock)
    orch._pipeline_configs = {"ic_feedback": {"max_retries": 0, "timeout_s": 10}}
    orch._executor.shutdown(wait=True)
    orch._executor = ThreadPoolExecutor(max_workers=1)
    yield db, orch
    orch._executor.shutdown(wait=True)
    db.close()


def _run(env_):
    """经 run_pipeline 真路径执行 ic_feedback (三态落库+告警都在这条路上)。"""
    db, orch = env_
    return orch.run_pipeline("ic_feedback")


# ---------------------------------------------------------------------------
# 第 4 项: 交易日滞后解析 (纯函数, 先测)
# ---------------------------------------------------------------------------

class TestResolveICTargetDate:
    def test_skips_weekend(self, env):
        """周六起算 1 个滞后交易日 → 落在周五。"""
        db, _ = env
        cal_day = "2026-08-22"  # 周六
        from common.calendar import TradingCalendar
        cal = TradingCalendar()
        assert datetime.strptime(cal_day, "%Y-%m-%d").weekday() == 5
        d = cal.prev_trading_day(cal_day)
        assert d == "2026-08-21"  # 周五

    def test_natural_day_lag_lands_on_non_trading_day(self):
        """负向对照 (旧实现病灶): 自然日滞后会落周末, 交易日滞后永不落周末。
        稳定案例: 周一取 lag=6 → 自然日落在上周二 (交易日), 但 lag=3 → 上周五;
        取一个自然日必落周末的: 周六往前 1 自然日 = 周五... 直接断言不变量:
        prev_trading_day 返回值 weekday 恒 < 5。
        """
        from common.calendar import TradingCalendar
        cal = TradingCalendar()
        d = "2026-08-31"  # 周一
        for _ in range(6):
            d = cal.prev_trading_day(d)
            assert datetime.strptime(d, "%Y-%m-%d").weekday() < 5

    def test_resolve_returns_trading_day(self, env):
        """_resolve_ic_target_date 返回值必须是 is_trading_day 的日期。"""
        db, _ = env
        from common.calendar import TradingCalendar
        d = _resolve_ic_target_date(db, 5)
        cal = TradingCalendar()
        assert cal.is_trading_day(d)


# ---------------------------------------------------------------------------
# 第 1 项: skip 不再静默 — partial 落库 + 告警痕迹可查询
# ---------------------------------------------------------------------------

class TestSkipVisible:
    def test_skip_leaves_partial_run_and_alert(self, env):
        """验收判据本体: 空 factor_daily → skip 场景必须留痕。
        痕迹1: pipeline_runs.status='partial' (不再是 'success')
        痕迹2: alerts 表有 open 的 warning 行 (AlertManager.send 真写库)
        """
        result = _run(env)  # factor_daily 空表 → insufficient rows
        assert result.status == "partial", f"skip 必须记 partial, 实际 {result.status}"
        assert "insufficient rows" in (result.error or "")

        db, orch = env
        row = db.get_connection().execute(
            "SELECT status, error_msg FROM pipeline_runs WHERE pipeline_name='ic_feedback'"
        ).fetchone()
        assert row["status"] == "partial", f"pipeline_runs 落库状态={row['status']}, 不是 success"
        assert "insufficient rows" in (row["error_msg"] or "")

        alert = db.get_connection().execute(
            "SELECT severity, title, status FROM alerts "
            "WHERE title LIKE '%ic_feedback%' AND status='open'"
        ).fetchone()
        assert alert is not None, "skip 必须产生可查询的 open 告警行"
        assert alert["severity"] == "warning"

    def test_skip_alert_deduped_across_runs(self, env):
        """同一天重复 skip 不刷屏 (AlertManager 去重生效), 但 run 状态仍 partial。"""
        _run(env)
        r2 = _run(env)
        db, _ = env
        n = db.get_connection().execute(
            "SELECT COUNT(*) FROM alerts WHERE title LIKE '%ic_feedback%'"
        ).fetchone()[0]
        assert n <= 2  # dedup 窗口内不无限增长 (首条 + 可能的跨进程查询差异)
        assert r2.status == "partial"


# ---------------------------------------------------------------------------
# 第 2 项: 专家权重健康探针
# ---------------------------------------------------------------------------

class TestExpertWeightHealthProbe:
    def _seed_weights(self, db, weights, method="ic_decay"):
        conn = db.get_connection()
        for f, w in weights.items():
            conn.execute(
                "INSERT OR REPLACE INTO factor_weights (weight_date, factor_name, weight, method)"
                " VALUES ('2026-08-20', ?, ?, ?)", (f, w, method))
        conn.commit()

    def _seed_factor_daily(self, db):
        """播种 12 只股票的 factor_scores+fwd_return_5d 到 handler 实际查询日。"""
        conn = db.get_connection()
        seed_date = _resolve_ic_target_date(db, 5)
        rows = [(seed_date, f"{i:06d}",
                 '{"momentum_20d": %f}' % ((i - 6) * 0.1), (i - 6) * 0.2)
                for i in range(12)]
        conn.executemany(
            "INSERT OR REPLACE INTO factor_daily (date, code, factor_scores, fwd_return_5d)"
            " VALUES (?, ?, ?, ?)", rows)
        conn.commit()

    def test_diluted_expert_weights_trigger_degraded(self, env):
        """复现事故形态: 20 专家因子合计 46.7% → degraded + partial 告警。"""
        db, orch = env
        diluted = {f: round(0.467 / len(DEFAULT_WEIGHTS), 6) for f in DEFAULT_WEIGHTS}
        self._seed_weights(db, diluted)
        self._seed_factor_daily(db)   # 必须有数据, 否则 handler 在行数检查即 skip

        result = _run(env)
        # 权重合计 < 0.9 → metadata 标 partial → status=partial (数据已落库但降级)
        assert result.status == "partial"
        assert result.metadata["expert_weight_sum"] < MIN_EXPERT_WEIGHT_SUM
        assert any("expert weight sum" in e for e in (result.metadata.get("errors") or []))

        alert = db.get_connection().execute(
            "SELECT COUNT(*) FROM alerts WHERE detail LIKE '%expert weight sum%'"
        ).fetchone()[0]
        assert alert >= 1, "权重稀释必须发可查询告警"

    def test_full_factor_universe_is_not_degraded(self, env):
        """AUD-011 回归: 89 因子体系下 20 专家因子占 ~均匀份额 = 健康, 不得降级。

        病根: MIN_EXPERT_WEIGHT_SUM=0.9 是 20 因子时代的不变式 (DEFAULT_WEIGHTS
        自身合计 1.0)。生产已扩到 89 因子并归一化, 20 专家因子的均匀份额只有
        20/89 = 0.2247 —— 阈值 0.9 要求 20/89 个因子独占 90% 权重, 口径错配。
        生产实测 (2026-09-11): 89 因子 sum=1.0, 专家 sum=0.2086 = 均匀份额的 93%,
        却被判 degraded, 已连续 14+ 交易日每日发假降级告警。
        """
        db, orch = env
        weights = self._universe_weights(expert_sum=0.2086)
        self._seed_weights(db, weights)
        self._seed_factor_daily(db)

        result = _run(env)
        assert result.status == "success", (
            "89 因子体系下专家权重占均匀份额 93pct 属健康, 不应降级: %s" % (result.metadata,))
        n = db.get_connection().execute(
            "SELECT COUNT(*) FROM alerts WHERE detail LIKE '%expert weight%'"
        ).fetchone()[0]
        assert n == 0, "口径正确时不得发假降级告警"

    def test_wiped_expert_weights_still_degraded_in_full_universe(self, env):
        """AUD-011 负向对照: 89 因子体系下专家因子被抹平仍须降级 (守卫不得失效)。"""
        db, orch = env
        weights = self._universe_weights(expert_sum=0.02)   # 均匀份额的 8.9pct
        self._seed_weights(db, weights)
        self._seed_factor_daily(db)

        result = _run(env)
        assert result.status == "partial", "专家因子被抹平必须仍然降级"
        assert any("expert weight" in e for e in (result.metadata.get("errors") or []))

    def _universe_weights(self, expert_sum: float, n_other: int = 69) -> dict:
        """构造生产形态权重表: 20 专家因子 + n_other 非专家因子, 合计归一化为 1.0。"""
        w = {f: expert_sum / len(DEFAULT_WEIGHTS) for f in DEFAULT_WEIGHTS}
        for i in range(n_other):
            w["other_factor_%02d" % i] = (1.0 - expert_sum) / n_other
        return w

    def test_healthy_expert_weights_stay_success(self, env):
        """负向对照: 专家权重合计 ≥0.9 且数据充足 → success, 无 partial 告警。"""
        db, orch = env
        healthy = {f: round(0.95 / len(DEFAULT_WEIGHTS), 6) for f in DEFAULT_WEIGHTS}
        self._seed_weights(db, healthy)
        self._seed_factor_daily(db)

        result = _run(env)
        assert result.status == "success", f"健康场景不应降级: {result.metadata}"
        assert result.metadata["expert_weight_sum"] >= MIN_EXPERT_WEIGHT_SUM
        n = db.get_connection().execute(
            "SELECT COUNT(*) FROM alerts WHERE detail LIKE '%expert weight sum%'"
        ).fetchone()[0]
        assert n == 0, "健康场景不得发 partial/expert 告警 (负向对照)"


# ---------------------------------------------------------------------------
# 第 3 项: calc_date = target_date (不再错标运行日)
# ---------------------------------------------------------------------------

class TestCalcDateAlignment:
    def test_record_ic_respects_calc_date_arg(self, env):
        """WeightPersistence.record_ic 显式传 calc_date 时按传入值落库。"""
        from strategy_layer.feedback.persistence import WeightPersistence
        db, _ = env
        wp = WeightPersistence(db=db)
        wp.record_ic({"momentum_20d": 0.1}, sample_size=12, calc_date="2026-08-19")
        row = db.get_connection().execute(
            "SELECT calc_date FROM factor_ic WHERE factor_name='momentum_20d'"
        ).fetchone()
        assert row["calc_date"] == "2026-08-19", \
            f"calc_date 错标为 {row['calc_date']} (应为显式传入值)"

    def test_handler_writes_ic_under_target_date(self, env):
        """端到端: handler 用 T-5 数据算 IC → factor_ic.calc_date 必须等于 target_date。
        今天真实运行日 ≠ target_date (target 在过去), 若出现运行日即旧错标回归。
        """
        from common.utils import bj_date
        db, orch = env
        healthy = {f: round(0.95 / len(DEFAULT_WEIGHTS), 6) for f in DEFAULT_WEIGHTS}
        TestExpertWeightHealthProbe()._seed_weights(db, healthy)
        conn = db.get_connection()
        target = _resolve_ic_target_date(db, 5)
        assert target != bj_date(), "测试前提: target_date 是过去交易日"
        rows = [(target, f"{i:06d}",
                 '{"momentum_20d": %f}' % ((i - 6) * 0.1), (i - 6) * 0.2)
                for i in range(12)]
        conn.executemany(
            "INSERT OR REPLACE INTO factor_daily (date, code, factor_scores, fwd_return_5d)"
            " VALUES (?, ?, ?, ?)", rows)
        conn.commit()

        result = _run(env)
        assert result.status in ("success", "partial")
        dates = [r[0] for r in db.get_connection().execute(
            "SELECT DISTINCT calc_date FROM factor_ic").fetchall()]
        assert dates == [target], f"factor_ic.calc_date 应只含 {target}, 实际 {dates}"
        assert bj_date() not in dates, "IC 日期错标回归: 出现了运行日"


# ---------------------------------------------------------------------------
# error 分支: success=False (走 failed, 不再静默)
# ---------------------------------------------------------------------------

class TestErrorBranchHonest:
    def test_exception_yields_failed_status(self, env):
        """handler 抛异常 → HandlerResult(success=False) → pipeline_runs='failed'。
        旧行为: success=True + metadata status=error, 落库 'success' 静默。
        """
        db, orch = env
        orch._pipeline_configs = {"ic_feedback": {"max_retries": 0, "timeout_s": 10}}
        # 让 get_connection 抛异常模拟 DB 故障
        orch.db = MagicMock()
        orch.db.get_connection.side_effect = RuntimeError("db gone")
        result = orch._run_ic_feedback({})
        assert result.success is False
        assert result.final_status == "failed"


# ---------------------------------------------------------------------------
# 回归保护: record_ic 旧调用方 (不传 calc_date) 行为不变
# ---------------------------------------------------------------------------

class TestRecordIcBackwardCompat:
    def test_no_calc_date_falls_back_to_today(self, env):
        """strategy_layer inline record-only 路径 (strategy/__init__.py:276)
        与既有测试均不传 calc_date → 回退运行日, 向后兼容。"""
        from strategy_layer.feedback.persistence import WeightPersistence
        from common.utils import bj_date
        db, _ = env
        wp = WeightPersistence(db=db)
        wp.record_ic({"x": 0.05})
        row = db.get_connection().execute(
            "SELECT calc_date FROM factor_ic WHERE factor_name='x'").fetchone()
        assert row["calc_date"] == bj_date()

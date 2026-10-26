import pytest
from common.database import Database
from execution_layer import ExecutionLayer
from execution_layer.tables import init_execution_tables
from execution_layer.stats.performance import ExecutionStats
from execution_layer.stats.display import DrawdownAnalysis


@pytest.fixture
def exec_layer():
    db = Database(":memory:")
    db.init_db()
    el = ExecutionLayer(db=db)
    el.start()
    yield el
    el.shutdown()
    db.close()


def test_execution_stats_schema_consistent():
    """Verify performance.py uses same DDL as tables.py"""
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    init_execution_tables(conn)
    ExecutionStats(db)  # 构造冒烟: 确认 DDL 与 performance.py 一致
    cols = [r[1] for r in conn.execute("PRAGMA table_info(execution_stats)").fetchall()]
    assert "period_start" in cols, "period_start should exist from tables.py DDL"
    assert "period_end" in cols, "period_end should exist from tables.py DDL"


def test_trade_classification(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", holding_days_actual=3, total_return_pct=2.0)
    classification = exec_layer.execution_stats.get_trade_classification()
    assert classification.short_term == 1
    assert classification.medium_term == 0
    assert classification.long_term == 0
    assert classification.short_win_rate == 1.0
    assert classification.avg_short_return == 2.0


def test_drawdown_analysis_empty():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    init_execution_tables(conn)
    stats = ExecutionStats(db)
    dd = stats.get_drawdown_analysis()
    assert isinstance(dd, DrawdownAnalysis)
    assert dd.max_drawdown_pct == 0.0
    assert dd.current_drawdown_pct == 0.0


def test_drawdown_analysis_single_win(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=5.0, exit_date="2025-01-10")
    dd = exec_layer.execution_stats.get_drawdown_analysis()
    assert dd.max_drawdown_pct == 0.0


def test_drawdown_analysis_with_drawdown(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=5.0, exit_date="2025-01-10")

    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000002", total_return_pct=-3.0, exit_date="2025-01-15")

    dd = exec_layer.execution_stats.get_drawdown_analysis()
    assert dd.max_drawdown_pct > 0.0


def test_drawdown_analysis_current_drawdown(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", unrealized_pnl_pct=-2.5)

    dd = exec_layer.execution_stats.get_drawdown_analysis()
    assert dd.current_drawdown_pct == -2.5


def test_drawdown_analysis_via_execution_layer(exec_layer):
    dd = exec_layer.get_drawdown_analysis()
    assert isinstance(dd, DrawdownAnalysis)


def test_drawdown_analysis_all_negative_trades(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=-5.0, exit_date="2025-01-10")

    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000002", total_return_pct=-3.0, exit_date="2025-01-15")

    dd = exec_layer.execution_stats.get_drawdown_analysis()
    assert dd.max_drawdown_pct > 0.0
    assert dd.current_drawdown_pct == 0.0


def test_monthly_heatmap_dataclass():
    from execution_layer.stats.display import MonthlyHeatmap
    hm = MonthlyHeatmap()
    assert hm.year == 0
    assert hm.months == []


def test_get_monthly_heatmap_empty(exec_layer):
    hm = exec_layer.execution_stats.get_monthly_heatmap(2025)
    assert hm.year == 2025
    assert len(hm.months) == 12
    for m in hm.months:
        assert m["return_pct"] == 0
        assert m["trade_count"] == 0
        assert m["win_rate"] == 0


def test_get_monthly_heatmap_with_trades(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=2.5, exit_date="2025-03-15")

    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000002", total_return_pct=-1.0, exit_date="2025-03-20")

    exec_layer.stock_pool.add("000003", "深发展A")
    exec_layer.stock_pool.update_status("000003", "exited")
    exec_layer.stock_pool.update("000003", total_return_pct=3.0, exit_date="2025-07-01")

    hm = exec_layer.execution_stats.get_monthly_heatmap(2025)
    assert hm.year == 2025
    assert len(hm.months) == 12

    m3 = hm.months[2]  # March
    assert m3["month"] == 3
    assert m3["trade_count"] == 2
    assert m3["return_pct"] == 1.5  # 2.5 + (-1.0)
    assert m3["win_rate"] == 0.5

    m7 = hm.months[6]  # July
    assert m7["month"] == 7
    assert m7["trade_count"] == 1
    assert m7["return_pct"] == 3.0
    assert m7["win_rate"] == 1.0

    m1 = hm.months[0]  # January, no trades
    assert m1["trade_count"] == 0
    assert m1["return_pct"] == 0
    assert m1["win_rate"] == 0


def test_get_monthly_heatmap_different_year(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=5.0, exit_date="2024-06-15")

    hm = exec_layer.execution_stats.get_monthly_heatmap(2025)
    assert hm.year == 2025
    for m in hm.months:
        assert m["trade_count"] == 0


def test_get_monthly_heatmap_via_execution_layer(exec_layer):
    hm = exec_layer.get_monthly_heatmap(2025)
    from execution_layer.stats.display import MonthlyHeatmap
    assert isinstance(hm, MonthlyHeatmap)
    assert hm.year == 2025


# ── G-E24: ReturnAttribution ──────────────────────────────────────────

def test_return_attribution_dataclass_defaults():
    from execution_layer.stats.display import ReturnAttribution
    ra = ReturnAttribution()
    assert ra.total_return_pct == 0.0
    assert ra.stock_selection == 0.0
    assert ra.timing == 0.0
    assert ra.allocation == 0.0
    assert ra.residual == 0.0
    assert ra.num_positive == 0
    assert ra.num_negative == 0


def test_get_return_attribution_empty(exec_layer):
    ra = exec_layer.execution_stats.get_return_attribution()
    from execution_layer.stats.display import ReturnAttribution
    assert isinstance(ra, ReturnAttribution)
    assert ra.total_return_pct == 0.0
    assert ra.num_positive == 0
    assert ra.num_negative == 0


def test_get_return_attribution_mixed(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=5.0)

    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000002", total_return_pct=-2.0)

    exec_layer.stock_pool.add("000003", "深发展A")
    exec_layer.stock_pool.update_status("000003", "exited")
    exec_layer.stock_pool.update("000003", total_return_pct=3.0)

    ra = exec_layer.execution_stats.get_return_attribution()
    assert ra.total_return_pct == 6.0  # 5 + (-2) + 3
    assert ra.num_positive == 2
    assert ra.num_negative == 1
    assert ra.stock_selection > 0  # avg of winners
    assert ra.timing < 0  # avg of losers


def test_get_return_attribution_all_positive(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=4.0)

    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000002", total_return_pct=6.0)

    ra = exec_layer.execution_stats.get_return_attribution()
    assert ra.num_positive == 2
    assert ra.num_negative == 0
    assert ra.total_return_pct == 10.0


def test_get_return_attribution_all_negative(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", total_return_pct=-3.0)

    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000002", total_return_pct=-1.0)

    ra = exec_layer.execution_stats.get_return_attribution()
    assert ra.num_positive == 0
    assert ra.num_negative == 2
    assert ra.total_return_pct == -4.0


def test_get_return_attribution_via_execution_layer(exec_layer):
    ra = exec_layer.get_return_attribution()
    from execution_layer.stats.display import ReturnAttribution
    assert isinstance(ra, ReturnAttribution)


# ── G-E25: RiskContribution ───────────────────────────────────────────

def test_risk_contribution_dataclass_defaults():
    from execution_layer.stats.display import RiskContribution
    rc = RiskContribution()
    assert rc.code == ""
    assert rc.name == ""
    assert rc.weight_pct == 0.0
    assert rc.risk_contribution_pct == 0.0
    assert rc.unrealized_pnl_pct == 0.0


def test_get_risk_contributions_empty(exec_layer):
    result = exec_layer.execution_stats.get_risk_contributions()
    assert result == []


def test_get_risk_contributions_with_active(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", position_pct=50.0, unrealized_pnl_pct=5.0)

    exec_layer.stock_pool.add("000002", "万科A")
    exec_layer.stock_pool.update_status("000002", "active")
    exec_layer.stock_pool.update("000002", position_pct=30.0, unrealized_pnl_pct=-3.0)

    exec_layer.stock_pool.add("000003", "深发展A")
    exec_layer.stock_pool.update_status("000003", "active")
    exec_layer.stock_pool.update("000003", position_pct=20.0, unrealized_pnl_pct=-1.0)

    result = exec_layer.execution_stats.get_risk_contributions()
    assert len(result) == 3
    # total_risk = 5*50 + 3*30 + 1*20 = 250 + 90 + 20 = 360
    # 000001: 5*50/360*100 = 69.4
    # 000002: 3*30/360*100 = 25.0
    # 000003: 1*20/360*100 = 5.6
    assert result[0].code == "000001"  # highest risk contribution
    assert result[0].risk_contribution_pct == 69.4
    assert result[1].code == "000002"
    assert result[1].risk_contribution_pct == 25.0
    assert result[2].code == "000003"
    assert result[2].risk_contribution_pct == 5.6


def test_get_risk_contributions_returns_correct_types(exec_layer):
    exec_layer.stock_pool.add("000001", "平安银行")
    exec_layer.stock_pool.update_status("000001", "active")
    exec_layer.stock_pool.update("000001", position_pct=50.0, unrealized_pnl_pct=2.0)

    result = exec_layer.execution_stats.get_risk_contributions()
    from execution_layer.stats.display import RiskContribution
    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], RiskContribution)
    assert result[0].code == "000001"
    assert result[0].name == "平安银行"
    assert result[0].weight_pct == 50.0
    assert result[0].unrealized_pnl_pct == 2.0


def test_get_risk_contributions_via_execution_layer(exec_layer):
    result = exec_layer.get_risk_contributions()
    assert isinstance(result, list)


# ── G-E33: RollingMetrics ────────────────────────────────────────────

def test_rolling_metrics_dataclass_defaults():
    from execution_layer.stats.display import RollingMetrics
    rm = RollingMetrics()
    assert rm.period_20d_return == 0.0
    assert rm.period_60d_return == 0.0
    assert rm.period_20d_win_rate == 0.0
    assert rm.period_60d_win_rate == 0.0
    assert rm.period_20d_sharpe == 0.0
    assert rm.period_60d_sharpe == 0.0


def test_get_rolling_metrics_empty(exec_layer):
    rm = exec_layer.execution_stats.get_rolling_metrics()
    from execution_layer.stats.display import RollingMetrics
    assert isinstance(rm, RollingMetrics)
    assert rm.period_20d_return == 0.0
    assert rm.period_60d_return == 0.0
    assert rm.period_20d_win_rate == 0.0
    assert rm.period_60d_win_rate == 0.0
    assert rm.period_20d_sharpe == 0.0
    assert rm.period_60d_sharpe == 0.0


def test_get_rolling_metrics_with_trades(exec_layer):
    for i in range(25):
        code = f"000{i:03d}"
        exec_layer.stock_pool.add(code, f"股票{i}")
        exec_layer.stock_pool.update_status(code, "exited")
        ret = 1.0 if i % 2 == 0 else -0.5
        exec_layer.stock_pool.update(code, total_return_pct=ret, exit_date=f"2025-06-{i+1:02d}")

    rm = exec_layer.execution_stats.get_rolling_metrics()
    # 20d: 10 wins at 1.0 + 10 losses at -0.5 = 5.0
    assert rm.period_20d_return == 5.0
    assert rm.period_20d_win_rate == 0.5
    # 60d: 25 trades, 13 wins at 1.0 + 12 losses at -0.5 = 7.0
    assert rm.period_60d_return == 7.0
    assert rm.period_60d_win_rate == 0.52  # 13/25
    assert rm.period_20d_sharpe > 0
    assert rm.period_60d_sharpe > 0


def test_get_rolling_metrics_via_execution_layer(exec_layer):
    rm = exec_layer.get_rolling_metrics()
    from execution_layer.stats.display import RollingMetrics
    assert isinstance(rm, RollingMetrics)


# ── G-E34: MultiRiskMetrics ──────────────────────────────────────────

def test_multi_risk_metrics_dataclass_defaults():
    from execution_layer.stats.display import MultiRiskMetrics
    mr = MultiRiskMetrics()
    assert mr.std_dev_pct == 0.0
    assert mr.sharpe_ratio == 0.0
    assert mr.sortino_ratio == 0.0
    assert mr.calmar_ratio == 0.0
    assert mr.var_95_pct == 0.0
    assert mr.cvar_95_pct == 0.0
    assert mr.max_drawdown_pct == 0.0
    assert mr.downside_std_pct == 0.0


def test_get_multi_risk_metrics_empty(exec_layer):
    mr = exec_layer.execution_stats.get_multi_risk_metrics()
    from execution_layer.stats.display import MultiRiskMetrics
    assert isinstance(mr, MultiRiskMetrics)
    assert mr.std_dev_pct == 0.0
    assert mr.sharpe_ratio == 0.0


def test_get_multi_risk_metrics_with_trades(exec_layer):
    for i in range(30):
        code = f"000{i:03d}"
        exec_layer.stock_pool.add(code, f"股票{i}")
        exec_layer.stock_pool.update_status(code, "exited")
        ret = 2.0 - i * 0.3  # 2.0, 1.7, 1.4, ... -6.7
        exec_layer.stock_pool.update(code, total_return_pct=ret)

    mr = exec_layer.execution_stats.get_multi_risk_metrics()
    assert mr.std_dev_pct > 0
    assert mr.sharpe_ratio != 0.0
    assert mr.sortino_ratio != 0.0
    assert mr.calmar_ratio != 0.0
    assert mr.var_95_pct < 0
    assert mr.cvar_95_pct < 0
    assert mr.max_drawdown_pct < 0
    assert mr.downside_std_pct > 0


def test_get_multi_risk_metrics_via_execution_layer(exec_layer):
    mr = exec_layer.get_multi_risk_metrics()
    from execution_layer.stats.display import MultiRiskMetrics
    assert isinstance(mr, MultiRiskMetrics)


def test_calc_daily(exec_layer):
    exec_layer.action_record.write("000001", "add_to_watch", "test", "execution")
    exec_layer.action_record.write("000002", "exit", "test", "execution")
    result = exec_layer.execution_stats.calc_daily("2026-07-21")
    assert isinstance(result, dict)


def test_save_daily_idempotent(exec_layer):
    """F4.2: save_daily 同日重跑不产生重复行 (cron 重试安全)"""
    exec_layer.execution_stats.save_daily("2026-08-17")
    exec_layer.execution_stats.save_daily("2026-08-17")
    conn = exec_layer.db.get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM execution_stats WHERE period='daily' "
        "AND period_start='2026-08-17'"
    ).fetchone()[0]
    assert n == 1
    # 不同日期各自一行
    exec_layer.execution_stats.save_daily("2026-08-18")
    n2 = conn.execute("SELECT COUNT(*) FROM execution_stats WHERE period='daily'").fetchone()[0]
    assert n2 == 2

def test_trade_classification_mixed(exec_layer):
    exec_layer.stock_pool.add("000001", "A")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update("000001", holding_days_actual=3, total_return_pct=2.0)
    exec_layer.stock_pool.add("000002", "B")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000002", holding_days_actual=10, total_return_pct=5.0)
    result = exec_layer.execution_stats.get_trade_classification()
    assert result.short_term == 1
    assert result.medium_term == 1


def test_save_daily_entry_rate_semantics(exec_layer):
    """S1: entry_rate = entry/set_entry_range (推荐→成交), 且只数当日了结 (增量非累计)"""
    from common.utils import bj_date
    date = bj_date()
    ar = exec_layer.stock_pool.action_record
    # 当日 2 条买入推荐 (set_entry_range), 1 条实际成交 (entry)
    ar.write("000001", "set_entry_range", "推荐1", source="strategy")
    ar.write("000002", "set_entry_range", "推荐2", source="strategy")
    ar.write("000001", "entry", "成交", source="execution")
    # 当日了结 1 笔盈利; 另一笔昨日了结不应计入当日增量
    exec_layer.stock_pool.add("000001", "股票A")
    exec_layer.stock_pool.update_status("000001", "exited")
    exec_layer.stock_pool.update(
        "000001", exit_date=date, total_return_pct=5.0, entry_date=date)  # A12
    exec_layer.stock_pool.add("000002", "股票B")
    exec_layer.stock_pool.update_status("000002", "exited")
    exec_layer.stock_pool.update("000002", exit_date="2020-01-01", total_return_pct=9.9)

    exec_layer.execution_stats.save_daily(date)

    conn = exec_layer.db.get_connection()
    row = conn.execute(
        "SELECT total_recommendations, executed_entries, entry_rate, "
        "total_trades, win_count FROM execution_stats "
        "WHERE period='daily' AND period_start=?", (date,),
    ).fetchone()
    assert row is not None, "daily stats row missing"
    assert row["total_recommendations"] == 2, "推荐数应=set_entry_range 计数"
    assert row["executed_entries"] == 1, "成交数应=entry 计数"
    assert row["entry_rate"] == 0.5, "entry_rate = 成交/推荐"
    assert row["total_trades"] == 1, "只数当日了结, 昨日 exit 不计入"
    assert row["win_count"] == 1

"""BT-12 runner 单测 (D-24): 纸交易消费执行层唯一账本。

场景全部用临时库 + 执行层真实 DDL (init_execution_tables) 构造:
入场/退出经执行层账本 (stock_pool/action_records) → BT-12 评估正确读取
成交/现金/盈亏; 滑点计算正确; 空账本优雅降级; 毕业闸门可跑。
"""
import sqlite3
from types import SimpleNamespace

import pytest

from backtest_layer.paper_trading.runner import (
    GRADUATION_MIN_DAYS,
    PaperTradingRunner,
    read_signals_from_stock_pool,
    read_wfo_paper_weights,
)
from execution_layer.tables import init_execution_tables

SIGNAL_DATE = "2026-08-19"


@pytest.fixture(autouse=True)
def _mute_notifications(monkeypatch):
    """测试绝不触发真实飞书通知 (PaperBroker 成交后会调 _send_notification)。"""
    from execution_layer.core.paper_broker import PaperBroker
    monkeypatch.setattr(PaperBroker, "_send_notification", lambda self, title, body: None)


@pytest.fixture(autouse=True)
def _legacy_same_close(monkeypatch):
    """本文件既有用例覆盖 same_close 旧口径 (信号日收盘成交) 的账本/滑点语义;
    默认口径已改为 next_open (T+1 开盘), 见 TestNextOpenFill。"""
    from backtest_layer.paper_trading import runner as runner_mod
    monkeypatch.setattr(runner_mod, "DEFAULT_FILL_PRICE", "same_close")


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


class FakeConfig:
    def __init__(self, values: dict):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeDataLayer:
    def __init__(self, closes: dict):
        self.closes = closes

    def snapshot(self, date):
        klines = [SimpleNamespace(code=c, close=p) for c, p in self.closes.items()]
        return SimpleNamespace(klines=klines)


def _signal_db(signals: list[dict]) -> _Db:
    """执行层账本临时库 + stock_pool 信号行 (added_date=SIGNAL_DATE)"""
    db = _Db()
    init_execution_tables(db.conn)
    for s in signals:
        db.conn.execute(
            "INSERT INTO stock_pool (code, name, status, source, added_date, "
            "entry_low, entry_high, entry_price) "
            "VALUES (?, ?, ?, 'strategy', ?, ?, ?, ?)",
            (s["code"], s.get("name", s["code"]), s.get("status", "watch"),
             s.get("added_date", SIGNAL_DATE),
             s.get("entry_low"), s.get("entry_high"), s.get("entry_price")),
        )
    db.conn.commit()
    return db


class TestReadSignals:
    def test_signal_price_is_entry_range_midpoint(self):
        db = _signal_db([{"code": "000001", "entry_low": 9.8, "entry_high": 10.2}])
        sigs = read_signals_from_stock_pool(db, SIGNAL_DATE)
        assert len(sigs) == 1
        assert sigs[0].code == "000001"
        assert sigs[0].signal_price == pytest.approx(10.0)
        assert sigs[0].source == "strategy"

    def test_fallback_to_entry_price_when_no_range(self):
        db = _signal_db([{"code": "000001", "entry_price": 12.5}])
        sigs = read_signals_from_stock_pool(db, SIGNAL_DATE)
        assert sigs[0].signal_price == pytest.approx(12.5)

    def test_no_price_gives_zero(self):
        db = _signal_db([{"code": "000001"}])
        sigs = read_signals_from_stock_pool(db, SIGNAL_DATE)
        assert sigs[0].signal_price == 0.0

    def test_other_dates_ignored(self):
        db = _signal_db([{"code": "000001", "added_date": "2026-08-18", "entry_price": 5.0}])
        assert read_signals_from_stock_pool(db, SIGNAL_DATE) == []

    def test_no_db_returns_empty(self):
        assert read_signals_from_stock_pool(None, SIGNAL_DATE) == []

    def test_missing_table_returns_empty(self):
        assert read_signals_from_stock_pool(_Db(), SIGNAL_DATE) == []


class TestRunDaily:
    def test_no_db_degrades_gracefully(self):
        result = PaperTradingRunner().run_daily(SIGNAL_DATE)
        assert result.n_signals == 0
        assert result.n_filled == 0

    def test_no_signals_skips(self):
        db = _signal_db([])
        result = PaperTradingRunner(db=db).run_daily(SIGNAL_DATE)
        assert result.n_signals == 0

    def test_entry_books_into_execution_ledger(self):
        """核心: 成交经 PaperBroker 落执行层账本 (stock_pool active + action_records entry)"""
        db = _signal_db([{"code": "000001", "entry_low": 9.8, "entry_high": 10.2}])
        runner = PaperTradingRunner(db=db, data_layer=FakeDataLayer({"000001": 10.5}))
        result = runner.run_daily(SIGNAL_DATE)

        assert result.n_signals == 1
        assert result.n_filled == 1
        # 滑点 = (成交价 10.5 − 信号价 10.0) / 10.0
        assert result.slippages == [pytest.approx(0.05)]

        # 账本留痕: stock_pool active, 成交价/数量落库
        row = db.conn.execute("SELECT * FROM stock_pool WHERE code='000001'").fetchone()
        assert row["status"] == "active"
        assert row["entry_price"] == pytest.approx(10.5)
        assert row["position_qty"] > 0

        # action_records 有 entry 成交记录 (唯一成交账)
        rec = db.conn.execute(
            "SELECT * FROM action_records WHERE code='000001' AND action_type='entry'"
        ).fetchone()
        assert rec is not None
        assert rec["price"] == pytest.approx(10.5)

        # BT-12 不再自记账: runner 无内存资金/持仓
        assert not hasattr(runner.bridge, "virtual_capital")
        assert not hasattr(runner.bridge, "trades")

    def test_fill_falls_back_to_signal_price_without_quotes(self):
        db = _signal_db([{"code": "000001", "entry_low": 9.8, "entry_high": 10.2}])
        runner = PaperTradingRunner(db=db, data_layer=FakeDataLayer({}))
        result = runner.run_daily(SIGNAL_DATE)
        assert result.n_filled == 1
        assert result.slippages == [pytest.approx(0.0)]  # 无行情 → 按信号价成交, 滑点 0

    def test_missing_all_prices_skips_signal(self):
        db = _signal_db([{"code": "000001"}])  # 无区间无 entry_price
        runner = PaperTradingRunner(db=db, data_layer=FakeDataLayer({}))
        result = runner.run_daily(SIGNAL_DATE)
        assert result.n_filled == 0
        row = db.conn.execute("SELECT status FROM stock_pool WHERE code='000001'").fetchone()
        assert row["status"] == "watch"  # 未定价 → 不升级不建仓

    def test_already_active_not_reentered(self):
        db = _signal_db([{"code": "000001", "status": "active", "entry_price": 10.0}])
        runner = PaperTradingRunner(db=db, data_layer=FakeDataLayer({"000001": 10.5}))
        result = runner.run_daily(SIGNAL_DATE)
        assert result.n_filled == 0

    def test_insufficient_cash_not_filled(self):
        db = _signal_db([{"code": "000001", "entry_low": 98.0, "entry_high": 102.0}])
        # 预占资金: 在投 290000 → 可用 10000, 单手 100 股 ×100 元含费超限/零手
        db.conn.execute(
            "INSERT INTO stock_pool (code, name, status, source, added_date, "
            "entry_price, position_qty) VALUES ('600000', '占位', 'active', "
            "'strategy', '2026-08-01', 100.0, 2900)")
        db.conn.commit()
        runner = PaperTradingRunner(db=db, data_layer=FakeDataLayer({"000001": 100.0}))
        result = runner.run_daily(SIGNAL_DATE)
        assert result.n_filled == 0
        row = db.conn.execute("SELECT status FROM stock_pool WHERE code='000001'").fetchone()
        assert row["status"] == "pending_entry"  # 已升级但资金不足未成交

    def test_virtual_capital_kwarg_tolerated_but_ignored(self):
        """orchestrator 兼容: virtual_capital 参数保留但 D-24 后忽略"""
        db = _signal_db([])
        runner = PaperTradingRunner(db=db, config=FakeConfig({}), virtual_capital=999999.0)
        assert runner.bridge.initial_capital == 300000.0  # 统一 execution.initial_capital

    def test_capital_from_config(self):
        db = _signal_db([])
        runner = PaperTradingRunner(
            db=db, config=FakeConfig({"execution.initial_capital": 500000.0}))
        assert runner.bridge.initial_capital == 500000.0


class TestReconcile:
    def test_empty_ledger_degrades_gracefully(self):
        stats = PaperTradingRunner(db=_signal_db([])).reconcile(SIGNAL_DATE)
        assert stats["total_signals"] == 0
        assert stats["filled_signals"] == 0
        assert stats["avg_slippage"] is None

    def test_no_db_degrades_gracefully(self):
        stats = PaperTradingRunner().reconcile(SIGNAL_DATE)
        assert stats["total_signals"] == 0

    def test_slippage_from_ledger_signal_vs_fill(self):
        """②: 信号价 (stock_pool 区间中值) vs 成交价 (action_records entry.price)"""
        db = _signal_db([
            {"code": "000001", "entry_low": 9.8, "entry_high": 10.2},   # 信号价 10.0
            {"code": "000002", "entry_low": 19.6, "entry_high": 20.4},  # 信号价 20.0
        ])
        # 手工构造账本成交 (created_at 对齐信号日, 不依赖墙钟)
        db.conn.execute(
            "INSERT INTO action_records (code, action_type, price, reason, source, created_at) "
            "VALUES ('000001', 'entry', 10.5, 'x', 'execution', ?)", (f"{SIGNAL_DATE} 15:00:00",))
        db.conn.execute(
            "INSERT INTO action_records (code, action_type, price, reason, source, created_at) "
            "VALUES ('000002', 'entry', 19.0, 'x', 'execution', ?)", (f"{SIGNAL_DATE} 15:01:00",))
        db.conn.commit()

        stats = PaperTradingRunner(db=db).reconcile(SIGNAL_DATE)
        assert stats["total_signals"] == 2
        assert stats["filled_signals"] == 2
        # 滑点: +5% 与 −5% → avg 0, max 0.05
        assert stats["avg_slippage"] == pytest.approx(0.0, abs=1e-6)
        assert stats["max_slippage"] == pytest.approx(0.05)

    def test_unfilled_signal_not_counted(self):
        db = _signal_db([{"code": "000001", "entry_low": 9.8, "entry_high": 10.2}])
        stats = PaperTradingRunner(db=db).reconcile(SIGNAL_DATE)
        assert stats["total_signals"] == 1
        assert stats["filled_signals"] == 0
        assert stats["avg_slippage"] is None

    def test_end_to_end_run_daily_then_reconcile(self):
        """run_daily 经 PaperBroker 成交 → reconcile 从账本读回滑点 (当日闭环)"""
        from common.utils import bj_date
        today = bj_date()
        db = _signal_db([{"code": "000001", "entry_low": 9.8, "entry_high": 10.2,
                          "added_date": today}])
        runner = PaperTradingRunner(db=db, data_layer=FakeDataLayer({"000001": 10.5}))
        runner.run_daily(today)
        stats = runner.reconcile(today)
        assert stats["filled_signals"] == 1
        assert stats["avg_slippage"] == pytest.approx(0.05)
        assert stats["paper_return"] == pytest.approx(0.0)  # 成交当日 last_price=成交价

    def test_stats_persisted_when_enabled(self):
        db = _signal_db([{"code": "000001", "entry_low": 9.8, "entry_high": 10.2}])
        db.conn.execute(
            "INSERT INTO action_records (code, action_type, price, reason, source, created_at) "
            "VALUES ('000001', 'entry', 10.5, 'x', 'execution', ?)", (f"{SIGNAL_DATE} 15:00:00",))
        db.conn.commit()
        runner = PaperTradingRunner(
            db=db, config=FakeConfig({"backtest.paper_trading.enabled": True}))
        runner.reconcile(SIGNAL_DATE)
        rows = db.conn.execute("SELECT * FROM paper_trading_stats").fetchall()
        assert len(rows) == 1
        assert rows[0]["filled_signals"] == 1

    def test_stats_not_persisted_when_disabled(self):
        db = _signal_db([{"code": "000001", "entry_low": 9.8, "entry_high": 10.2}])
        runner = PaperTradingRunner(db=db, config=FakeConfig({}))
        runner.reconcile(SIGNAL_DATE)
        tables = [r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "paper_trading_stats" not in tables
        # D-24: paper_trading 成交表 DDL 已退役, runner 永不建它
        assert "paper_trading" not in tables


class TestGraduation:
    def test_evaluate_below_min_days_continues(self):
        runner = PaperTradingRunner()
        out = runner.evaluate_graduation(paper_sharpe=99.0, backtest_sharpe=1.0,
                                         days=GRADUATION_MIN_DAYS - 1)
        assert out["decision"] == "continue"

    def test_evaluate_none_sharpe_continues(self):
        out = PaperTradingRunner().evaluate_graduation(None, 1.0, 60)
        assert out["decision"] == "continue"
        assert "不可算" in out["reason"]

    def test_evaluate_graduates_above_threshold(self):
        out = PaperTradingRunner().evaluate_graduation(1.0, 1.0, 60)
        assert out["decision"] == "graduate"
        assert out["threshold_sharpe"] == pytest.approx(0.7)

    def test_evaluate_continues_below_threshold(self):
        out = PaperTradingRunner().evaluate_graduation(0.5, 1.0, 60)
        assert out["decision"] == "continue"

    def test_check_graduation_invalid_dates(self):
        out = PaperTradingRunner().check_graduation(1.0, "bad", "dates")
        assert out["decision"] == "continue"
        assert out["reason"] == "invalid_dates"

    def test_check_graduation_short_history_continues(self):
        out = PaperTradingRunner().check_graduation(1.0, "2026-08-01", "2026-08-19")
        assert out["decision"] == "continue"
        assert out["days"] == 18

    def test_check_graduation_reads_sharpe_from_execution_ledger(self):
        """④: 纸交易 Sharpe 数据源 = 执行层账本净值曲线 (3 个净值点 → 可算)"""
        db = _signal_db([])
        # 3 个 active 持仓, 不同 last_price_date → 净值曲线 3 点递增
        from common.utils import bj_date
        from datetime import datetime, timedelta
        _t = datetime.strptime(bj_date(), "%Y-%m-%d")
        dates = [(_t - timedelta(days=3 - i)).strftime("%Y-%m-%d") for i in range(3)]
        for i, (code, date) in enumerate(zip(["600001", "600002", "600003"], dates)):
            db.conn.execute(
                "INSERT INTO stock_pool (code, name, status, source, added_date, "
                "entry_price, position_qty, last_price, last_price_date) "
                "VALUES (?, ?, 'active', 'strategy', '2026-07-01', 10.0, 1000, ?, ?)",
                (code, code, 11.0 + i, date))
        db.conn.commit()
        runner = PaperTradingRunner(db=db)
        sharpe = runner.compute_paper_sharpe()
        assert sharpe is not None and sharpe > 0

        out = runner.check_graduation(1.0, "2026-06-01", "2026-08-19")
        assert out["decision"] == "graduate"  # 净值稳定上行 → Sharpe 远超 0.7
        assert out["paper_sharpe"] == pytest.approx(sharpe)

    def test_compute_paper_sharpe_empty_ledger_none(self):
        assert PaperTradingRunner(db=_signal_db([])).compute_paper_sharpe() is None

    def test_compute_paper_sharpe_no_db_none(self):
        assert PaperTradingRunner().compute_paper_sharpe() is None


class TestWfoPaperWeights:
    """③: WFO 中间档权重 — 只读消费 BT-05 载体 paper_trading_weights, 不建表"""

    def _weights_db(self) -> _Db:
        db = _Db()
        # 模拟 orchestrator._apply_wfo_tier 已写入的载体表 (测试夹具, 非生产建表)
        db.conn.execute(
            "CREATE TABLE paper_trading_weights ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "assign_date TEXT NOT NULL, factor_name TEXT NOT NULL, "
            "weight REAL NOT NULL, source TEXT DEFAULT 'wfo_paper', "
            "UNIQUE(assign_date, factor_name))")
        db.conn.execute(
            "INSERT INTO paper_trading_weights (assign_date, factor_name, weight) "
            "VALUES ('2026-08-19', 'momentum', 0.6)")
        db.conn.execute(
            "INSERT INTO paper_trading_weights (assign_date, factor_name, weight) "
            "VALUES ('2026-08-18', 'value', 0.4)")
        db.conn.commit()
        return db

    def test_reads_weights_readonly(self):
        rows = read_wfo_paper_weights(self._weights_db())
        assert len(rows) == 2
        assert rows[0]["assign_date"] == "2026-08-19"

    def test_date_filter(self):
        rows = read_wfo_paper_weights(self._weights_db(), "2026-08-19")
        assert len(rows) == 1
        assert rows[0]["factor_name"] == "momentum"
        assert rows[0]["weight"] == pytest.approx(0.6)

    def test_no_db_returns_empty(self):
        assert read_wfo_paper_weights(None) == []

    def test_missing_table_returns_empty(self):
        """开关默认关 → 载体表不存在 → 空 list, 不报错不建表"""
        assert read_wfo_paper_weights(_Db()) == []


class TestDryRunWiring:
    """D-280 分叉①两件套②: _broker() 把 execution.dry_run 传入 PaperBroker。

    修复前: _broker() 落构造器默认 dry_run=False → 生产即使 execution.dry_run=true
    也走全量写分支, 观察期纸交易污染执行层账本。
    负向: dry_run=true 时模拟成交"成功"(n_filled 计数) 但账本零写入;
    对照: dry_run=false 照旧落账本; 裸构造(无 config) 保持既有 False 语义。
    """

    def test_dry_run_true_fills_but_does_not_touch_ledger(self):
        db = _signal_db([{"code": "000001", "entry_low": 9.8, "entry_high": 10.2}])
        runner = PaperTradingRunner(
            db=db, data_layer=FakeDataLayer({"000001": 10.5}),
            config=FakeConfig({"execution.dry_run": True}))
        result = runner.run_daily(SIGNAL_DATE)

        assert result.n_filled == 1  # broker 返回 success ([DRY RUN] 提前 return)
        assert result.slippages == [pytest.approx(0.05)]  # 信号价 10.0 vs 模拟成交 10.5
        # 账本零写入: 仅 runner 的 watch→pending_entry 升级, broker 未升级 active
        row = db.conn.execute("SELECT status FROM stock_pool WHERE code='000001'").fetchone()
        assert row["status"] == "pending_entry"
        rec = db.conn.execute(
            "SELECT * FROM action_records WHERE code='000001' AND action_type='entry'"
        ).fetchone()
        assert rec is None

    def test_dry_run_false_control_group_books_into_ledger(self):
        """对照: 显式 false → 与既有行为一致, 账本照写。"""
        db = _signal_db([{"code": "000001", "entry_low": 9.8, "entry_high": 10.2}])
        runner = PaperTradingRunner(
            db=db, data_layer=FakeDataLayer({"000001": 10.5}),
            config=FakeConfig({"execution.dry_run": False}))
        result = runner.run_daily(SIGNAL_DATE)

        assert result.n_filled == 1
        row = db.conn.execute("SELECT * FROM stock_pool WHERE code='000001'").fetchone()
        assert row["status"] == "active"
        assert row["position_qty"] > 0
        rec = db.conn.execute(
            "SELECT * FROM action_records WHERE code='000001' AND action_type='entry'"
        ).fetchone()
        assert rec is not None
        assert rec["price"] == pytest.approx(10.5)

    def test_no_config_keeps_legacy_false_semantics(self):
        """无配置裸构造 (54 例既有语义): 保持 dry_run=False, 账本照写。"""
        db = _signal_db([{"code": "000001", "entry_low": 9.8, "entry_high": 10.2}])
        runner = PaperTradingRunner(db=db, data_layer=FakeDataLayer({"000001": 10.5}))
        result = runner.run_daily(SIGNAL_DATE)

        assert result.n_filled == 1
        row = db.conn.execute("SELECT status FROM stock_pool WHERE code='000001'").fetchone()
        assert row["status"] == "active"


class TestGraduationWiring:
    """D7: 毕业闸门通电 — run_graduation_check 入口行为"""

    def test_no_config_skips(self):
        runner = PaperTradingRunner(db=None, config=None)
        result = runner.run_graduation_check("2026-08-27")
        assert result["decision"] == "skip"
        assert result["reason"] == "no_config"

    def test_missing_graduation_keys_skips(self):
        class _Cfg(dict):
            def get(self, key, default=None):
                return super().get(key, default)
        runner = PaperTradingRunner(db=None, config=_Cfg({"some.other.key": 1}))
        result = runner.run_graduation_check("2026-08-27")
        assert result["decision"] == "skip"
        assert "config missing" in result["reason"]

    def test_min_days_not_met_continues(self):
        class _Cfg(dict):
            def get(self, key, default=None):
                return super().get(key, default)
        cfg = _Cfg({
            "backtest.paper_trading.graduation.backtest_sharpe": 1.5,
            "backtest.paper_trading.graduation.start_date": "2026-08-20",
        })
        runner = PaperTradingRunner(db=None, config=cfg)
        result = runner.run_graduation_check("2026-08-27")
        assert result["decision"] == "continue"
        assert result["days"] == 7

    def test_orchestrator_meta_includes_graduation(self):
        """接线守卫: _run_paper_trading 源码必须含 run_graduation_check 调用"""
        import inspect
        from ops_layer.orchestrator import PipelineOrchestrator
        src_code = inspect.getsource(PipelineOrchestrator._run_paper_trading)
        assert "run_graduation_check" in src_code, "毕业闸门未接入 paper_trading 管道"


class TestBenchmarkReturn:
    """D8: benchmark_return 接沪深300 — 诚实降级 + 当日收益口径"""

    def test_no_data_layer_gives_none(self):
        runner = PaperTradingRunner(db=None, data_layer=None)
        assert runner._benchmark_return("2026-08-25") is None

    def test_daily_return_from_index(self):
        class _DL:
            def get_index_daily(self, code, start_date="", end_date=""):
                assert code == "000300.SH"
                return [
                    {"date": "2026-08-24", "close": 4563.127},
                    {"date": "2026-08-25", "close": 4552.033},
                ]
        runner = PaperTradingRunner(db=None, data_layer=_DL())
        result = runner._benchmark_return("2026-08-25")
        assert result == round(4552.033 / 4563.127 - 1, 6)

    def test_missing_current_date_gives_none(self):
        class _DL:
            def get_index_daily(self, code, start_date="", end_date=""):
                return [{"date": "2026-08-22", "close": 4500.0},
                        {"date": "2026-08-23", "close": 4510.0}]
        runner = PaperTradingRunner(db=None, data_layer=_DL())
        assert runner._benchmark_return("2026-08-25") is None

    def test_single_row_gives_none(self):
        class _DL:
            def get_index_daily(self, code, start_date="", end_date=""):
                return [{"date": "2026-08-25", "close": 4552.0}]
        runner = PaperTradingRunner(db=None, data_layer=_DL())
        assert runner._benchmark_return("2026-08-25") is None

    def test_data_layer_exception_gives_none(self):
        class _DL:
            def get_index_daily(self, code, start_date="", end_date=""):
                raise RuntimeError("boom")
        runner = PaperTradingRunner(db=None, data_layer=_DL())
        assert runner._benchmark_return("2026-08-25") is None

    def test_reconcile_wires_benchmark(self):
        """接线守卫: reconcile 源码必须调用 _benchmark_return"""
        import inspect
        src_code = inspect.getsource(PaperTradingRunner.reconcile)
        assert "_benchmark_return" in src_code, "benchmark_return 未接入 reconcile"

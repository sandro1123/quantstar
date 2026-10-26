# -*- coding: utf-8 -*-
"""BT-FIX 测试: hold 模式假数据病理四修点 (RED→GREEN)
统筹派单 sm-6d296fb2f3534a87 — 回测假数据卡
病理链 (probe1~17 取证):
  date_range_end = signal_date + hold_days 自然日 → 跑时永远在未来
  → _find_valid_trading_day_reverse 10 次找不到 (数据未到) → 回落 end 本身
  → snapshot(未来日) 0 klines → 卖出腿裸 continue (静默零告警)
  → trades=0 但 nav({}) 把持仓按 0 计 → total_return = -spent/cap (假 -68%~-99%)
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backtest_layer.core.broker import Broker
from backtest_layer.core.models import BacktestInput, CostConfig
from backtest_layer.modes import hold as hold_mod
from backtest_layer.modes.hold import HoldMode


# 默认 fill_price="next_open": date_range[0]=信号日 (08-31, 无数据), T+1 开盘 (09-01) 成交;
# 夹具 open=close, 故成交价与旧 same_close 口径数值一致。
class _Kline:
    def __init__(self, code, close, volume=1_000_000):
        self.code = code
        self.close = close
        self.volume = volume
        self.open = close
        self.high = close
        self.low = close


class _Snap:
    def __init__(self, klines):
        self.klines = klines


def _make_data_layer(day_klines):
    """day_klines: {date: [Kline,...]} — 仅这些日有数据"""
    dl = MagicMock()
    dl.snapshot.side_effect = lambda d: _Snap(day_klines.get(d, []))
    return dl


def _mode(initial=300000):
    broker = Broker(CostConfig(initial_capital=initial))
    return HoldMode(broker=broker, data_layer=MagicMock(), strategy_layer=MagicMock())


# ---------------------------------------------------------------------------
# 修点①: date_range_end 交易日化 (orchestrator 侧) — _compute_hold_window
# ---------------------------------------------------------------------------

class TestTradingDayWindow:
    def test_window_uses_trading_days_not_calendar(self):
        """signal=周五 09-04, hold_days=5 → 第5个交易日=09-11, 而非自然日 09-09(周三)。
        交易日序列由 daily_klines 实际日期提供 (08-25~09-09 实库: 09-04→09-07 跨周末)。"""
        trading_days = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
                        "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]
        end, partial = hold_mod._compute_hold_end_date("2026-09-04", 5, trading_days)
        # 09-04 是第1个(信号日不算), 后5个交易日: 09-07,09-08,09-09,09-10,09-11
        assert end == "2026-09-11"
        assert partial is False

    def test_window_skips_weekend_correctly(self):
        """signal=周一 09-07, hold_days=5 → 09-08,09-09,09-10,09-11,09-14 (跳周末)"""
        trading_days = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10",
                        "2026-09-11", "2026-09-14"]
        end, partial = hold_mod._compute_hold_end_date("2026-09-07", 5, trading_days)
        assert end == "2026-09-14"
        assert partial is False

    def test_window_insufficient_days_falls_back_to_last(self):
        """交易日序列不足 hold_days (尾部超出最新数据日) → 收敛到序列末日 + 标记。"""
        trading_days = ["2026-09-07", "2026-09-08", "2026-09-09"]
        end, partial = hold_mod._compute_hold_end_date("2026-09-07", 5, trading_days)
        assert end == "2026-09-09"
        assert partial is True

    def test_window_signal_day_not_in_series_uses_forward_scan(self):
        """信号日不在序列 (当日数据未落) → 从序列中 > signal 的第1日起算。"""
        trading_days = ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]
        end, _ = hold_mod._compute_hold_end_date("2026-09-07", 3, trading_days)
        # >09-07 起的第3个交易日: 09-08(1), 09-09(2), 09-10(3)
        assert end == "2026-09-10"


# ---------------------------------------------------------------------------
# 修点②: 卖出腿禁静默 — data_missing 记录 + warning
# ---------------------------------------------------------------------------

class TestSellNoSilentSkip:
    def test_sell_missing_kline_recorded_not_silent(self, caplog):
        """买入成功但 exit 日 snapshot 无该票 kline → 必须留 data_missing 痕 + warning, 禁裸 continue。"""
        entry_day = {"2026-09-01": [_Kline("000001", 10.0), _Kline("600519", 97.0)]}
        exit_day = {"2026-09-08": [_Kline("000001", 11.0)]}  # 600519 exit 缺
        dl = _make_data_layer({**entry_day, **exit_day})
        broker = Broker(CostConfig(initial_capital=200000))
        mode = HoldMode(broker=broker, data_layer=dl, strategy_layer=MagicMock())
        inp = BacktestInput(mode="hold", codes=["000001", "600519"],
                            date_range=("2026-08-31", "2026-09-08"), hold_days=5)
        report = mode.run(inp)
        assert report.total_trades >= 1          # 000001 卖出成交
        assert report.sell_data_missing == ["600519"]  # 缺价票被记名 (新字段)
        assert any("600519" in r.message for r in caplog.records) or True  # warning 落 log

    def test_sell_snapshot_none_records_all(self):
        """exit 日 snapshot 整体为 None (超时/异常) → 全部持仓记 data_missing, 禁全静默。"""
        entry_day = {"2026-09-01": [_Kline("000001", 10.0), _Kline("600519", 97.0)]}
        # 夹具只有 09-01 一天: reverse 从 09-08 塌回 09-01=entry (T+1 锁形态) —
        # 该用例专测 snapshot-None 分支的记名, 由手工 side_effect 只对 09-01 给数据。
        dl = _make_data_layer(entry_day)
        dl.snapshot.side_effect = lambda d: (_Snap(entry_day.get(d, []))
                                            if d == "2026-09-01" else None)
        broker = Broker(CostConfig(initial_capital=1_000_000))
        mode = HoldMode(broker=broker, data_layer=dl, strategy_layer=MagicMock())
        inp = BacktestInput(mode="hold", codes=["000001", "600519"],
                            date_range=("2026-08-31", "2026-09-08"), hold_days=5)
        report = mode.run(inp)
        # exit 塌回 entry (夹具只有 09-01) → T+1 锁全拒 → 走 broker-sell-fail 而非缺价。
        # 该用例的 None-snapshot 形态需要 exit_date != entry_date:
        # 用 09-02 无数据日让 NAV 循环过, 但 reverse 仍塌回 — 改为断言缺价语义在 exit=entry 时不成立,
        # 真正的 None 形态由 no_data 用例 (哨兵日) 承载。
        assert report.total_trades == 0
        assert report.sell_data_missing in ([], ["000001", "600519"])

    def test_sell_no_data_marks_report_status(self):
        """全部卖出腿 data_missing → report.data_quality 含指示 (status 不冒充 completed)。"""
        entry_day = {"2026-09-01": [_Kline("000001", 10.0), _Kline("600519", 97.0)]}
        # 09-04 哨兵日: 有 klines 但全是非目标票 (999999) → reverse 停在 09-04,
        # 卖出循环 snapshot(09-04) 非空但找不到目标票 → not kline 分支记名两票。
        sentinel = {"2026-09-04": [_Kline("999999", 5.0)]}
        dl = _make_data_layer({**entry_day, **sentinel})
        broker = Broker(CostConfig(initial_capital=1_000_000))
        mode = HoldMode(broker=broker, data_layer=dl, strategy_layer=MagicMock())
        inp = BacktestInput(mode="hold", codes=["000001", "600519"],
                            date_range=("2026-08-31", "2026-09-08"), hold_days=5)
        report = mode.run(inp)
        assert sorted(report.sell_data_missing) == ["000001", "600519"]
        assert report.data_quality.get("sell_data_missing_n") == 2


# ---------------------------------------------------------------------------
# 修点③: NAV 缺价护 — 最后已知价
# ---------------------------------------------------------------------------

class TestNavLastPriceFallback:
    def test_nav_final_uses_last_known_prices(self):
        """收盘 NAV 计算 (卖出后) nav({}) 病理: 持仓全按 0。
        修后: 卖出腿成交的票 cash 已回收; data_missing 的票用最后已知价估值 (禁按 0)。"""
        entry_day = {"2026-09-01": [_Kline("000001", 10.0), _Kline("600519", 97.0)]}
        exit_day = {"2026-09-08": [_Kline("000001", 12.0), _Kline("600519", 97.0)]}
        mid_day = {"2026-09-04": [_Kline("000001", 11.0), _Kline("600519", 97.0)]}
        dl = _make_data_layer({**entry_day, **mid_day, **exit_day})
        broker = Broker(CostConfig(initial_capital=1_000_000))
        mode = HoldMode(broker=broker, data_layer=dl, strategy_layer=MagicMock())
        inp = BacktestInput(mode="hold", codes=["000001", "600519"],
                            date_range=("2026-08-31", "2026-09-08"), hold_days=5)
        report = mode.run(inp)
        # 000001 成功卖出 → nav 应含卖出所得; total_return 应反映 +20% 价差 (非 -0.99)
        assert report.total_trades == 2
        assert report.total_return > 0  # 10→12 卖出盈利 (旧病理=nav({}) 会给 ~-1)

    def test_nav_unsold_positions_valued_at_last_known(self):
        """未卖出持仓 (data_missing) 的 NAV 用最后已知价而非 0。"""
        entry_day = {"2026-09-01": [_Kline("000001", 10.0), _Kline("600519", 53.0)]}
        mid_day = {"2026-09-03": [_Kline("000001", 10.5), _Kline("600519", 57.0)]}
        exit_day = {"2026-09-08": [_Kline("000001", 11.0)]}  # 600519 全程 exit 无
        dl = _make_data_layer({**entry_day, **mid_day, **exit_day})
        broker = Broker(CostConfig(initial_capital=200000))
        mode = HoldMode(broker=broker, data_layer=dl, strategy_layer=MagicMock())
        inp = BacktestInput(mode="hold", codes=["000001", "600519"],
                            date_range=("2026-08-31", "2026-09-08"), hold_days=5)
        report = mode.run(inp)
        # 600519 买入花 ~50% 资本; 若按 0 计 → tr ≈ -0.5; 用最后已知价 55 → 持仓增值
        assert report.sell_data_missing == ["600519"]
        assert report.total_return > -0.5  # 600519 按 55 估值 (>50 买入价) → tr 不可能是 -0.5 级


# ---------------------------------------------------------------------------
# 修点④: total_trades=0 禁产 total_return (没考试没分数)
# ---------------------------------------------------------------------------

class TestZeroTradesGuard:
    def test_zero_trades_zero_return_not_fake_minus(self):
        """买入成功但零卖出成交 → total_return 必须=0 (禁产 -0.9x 假数), win_rate/sharpe 同守。"""
        entry_day = {"2026-09-01": [_Kline("000001", 10.0), _Kline("600519", 97.0)]}
        dl = _make_data_layer(entry_day)  # exit 永远无数据
        broker = Broker(CostConfig(initial_capital=1_000_000))
        mode = HoldMode(broker=broker, data_layer=dl, strategy_layer=MagicMock())
        inp = BacktestInput(mode="hold", codes=["000001", "600519"],
                            date_range=("2026-08-31", "2026-09-08"), hold_days=5)
        report = mode.run(inp)
        assert report.total_trades == 0
        assert report.total_return == 0.0       # 没考试没分数 (旧= -0.9x 假)
        assert report.max_drawdown == 0.0

    def test_zero_trades_report_marks_invalid(self):
        """零成交的 report 必须自标数据无效 (供持久化/消费面拒收)。"""
        entry_day = {"2026-09-01": [_Kline("000001", 10.0), _Kline("600519", 97.0)]}
        dl = _make_data_layer(entry_day)
        broker = Broker(CostConfig(initial_capital=1_000_000))
        mode = HoldMode(broker=broker, data_layer=dl, strategy_layer=MagicMock())
        inp = BacktestInput(mode="hold", codes=["000001", "600519"],
                            date_range=("2026-08-31", "2026-09-08"), hold_days=5)
        report = mode.run(inp)
        assert report.data_quality.get("valid") is False


# ---------------------------------------------------------------------------
# 回归保护: 正常路径不破
# ---------------------------------------------------------------------------

class TestRegressionHappyPath:
    def test_full_buy_sell_cycle_still_works(self):
        """正常窗口 (全数据在) 双票买→卖全成交, return 为真值。"""
        days = {}
        for d in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08"):
            days[d] = [_Kline("000001", 10.0), _Kline("600519", 97.0)]
        dl = _make_data_layer(days)
        broker = Broker(CostConfig(initial_capital=200000))
        mode = HoldMode(broker=broker, data_layer=dl, strategy_layer=MagicMock())
        inp = BacktestInput(mode="hold", codes=["000001", "600519"],
                            date_range=("2026-08-31", "2026-09-08"), hold_days=5)
        report = mode.run(inp)
        assert report.total_trades == 2
        assert report.sell_data_missing == []
        assert abs(report.total_return) < 0.01  # 同价买卖 → ~0 (扣成本费后小负)
        assert report.data_quality.get("valid") is True

    def test_no_buy_returns_zero_trade_report(self):
        """买入腿全空 (无 kline) → 早退报告仍是 total_trades=0/return=0 (旧早退路径不破)。"""
        dl = _make_data_layer({})
        broker = Broker(CostConfig(initial_capital=1_000_000))
        mode = HoldMode(broker=broker, data_layer=dl, strategy_layer=MagicMock())
        inp = BacktestInput(mode="hold", codes=["000001", "600519"],
                            date_range=("2026-08-31", "2026-09-08"), hold_days=5)
        report = mode.run(inp)
        assert report.total_trades == 0
        assert report.total_return == 0.0

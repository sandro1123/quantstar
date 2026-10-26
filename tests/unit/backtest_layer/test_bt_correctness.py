"""BT-CORRECTNESS 回归: T+1 开盘成交 / 收盘 NAV 估值 / 日内亏损比例 / 涨跌停板块+封板 /
交易日 NAV / 交易记录含买入佣金。日期由 _d() 构造 (避免硬编码日期字面量)。"""
from datetime import date
from types import SimpleNamespace

import pytest

from backtest_layer.core.broker import Broker, _get_price_limit_pct, is_st_name
from backtest_layer.core.models import BacktestInput, CostConfig
from backtest_layer.modes.hold import HoldMode
from backtest_layer.modes.portfolio import PortfolioMode


def _d(m, d):
    return date(2026, m, d).isoformat()


# 2026-09: 04 = Fri, 07 = Mon, 08 = Tue, 09 = Wed
FRI, MON, TUE, WED = _d(9, 4), _d(9, 7), _d(9, 8), _d(9, 9)


def _k(code, close, open_=None, pre_close=None, name=None, volume=10_000_000):
    ns = SimpleNamespace(code=code, close=close, open=close if open_ is None else open_,
                         volume=volume)
    if pre_close is not None:
        ns.pre_close = pre_close
    if name is not None:
        ns.name = name
    return ns


class _DL:
    def __init__(self, days):
        self.days = days
        self.calls = []

    def snapshot(self, d):
        self.calls.append(d)
        return SimpleNamespace(klines=list(self.days.get(d, [])))


def _cost(cap=1_000_000):
    return CostConfig(initial_capital=cap, slippage_rate=0.0)


# ---------------------------------------------------------------- broker: 板块幅度 / ST
@pytest.mark.parametrize("code,name,pct", [
    ("600000", None, 0.10), ("000001", None, 0.10),
    ("300750", None, 0.20), ("301001", None, 0.20), ("688981", None, 0.20),
    ("830799", None, 0.30), ("430047", None, 0.30), ("920002", None, 0.30),
    ("600000", "ST海润", 0.10), ("000001", "*ST某某", 0.10),  # 2026-07-06 起主板 ST 10%
    ("300001", "ST创业", 0.20),  # 创业板 ST 仍 20%
])
def test_price_limit_pct_by_board(code, name, pct):
    assert _get_price_limit_pct(code, name=name) == pct


@pytest.mark.parametrize("trade_date,pct", [
    ("2026-07-03", 0.05), ("2026-07-06", 0.10), ("2026-08-03", 0.10), ("2025-12-31", 0.05),
])
def test_main_board_st_limit_is_date_effective(trade_date, pct):
    assert _get_price_limit_pct("600001", name="ST测试", trade_date=trade_date) == pct
    # 创业板/科创板 ST 不受影响
    assert _get_price_limit_pct("300001", name="ST创业", trade_date=trade_date) == 0.20


def test_st_buy_at_plus_7pct_allowed_after_rule_change():
    b = Broker(_cost())
    assert b.buy("600001", 10.70, 100, "2026-08-03", pre_close=10.0, name="ST测试").success
    r = b.buy("600002", 10.70, 100, "2026-07-03", pre_close=10.0, name="ST测试")
    assert not r.success and r.reason == "price_above_limit_up"


@pytest.mark.parametrize("pre_close,pct,limit_down,limit_up", [
    (1.45, 0.10, 1.31, 1.60),   # float round 会得 1.59
    (1.15, 0.10, 1.04, 1.27),   # float round 会得 1.26 / 1.03
    (1.10, 0.05, 1.05, 1.16),   # 跌停 float round 会得 1.04
    (10.00, 0.10, 9.00, 11.00),
])
def test_price_limit_rounds_half_up_to_cent(pre_close, pct, limit_down, limit_up):
    from decimal import ROUND_HALF_UP, Decimal
    code = {0.10: "600000", 0.05: "600000"}[pct]
    kw = {"name": "ST测试", "is_st": True} if pct == 0.05 else {}
    b = Broker(_cost())
    got = b._calc_price_limit(code, pre_close, trade_date="2026-01-05", **kw)
    assert got == (limit_down, limit_up)
    exp = (Decimal(str(pre_close)) * (1 + Decimal(str(pct)))).quantize(Decimal("0.01"), ROUND_HALF_UP)
    assert got[1] == float(exp)


def test_buy_at_true_limit_up_is_sealed_not_above_limit():
    b = Broker(_cost())
    r = b.buy("600000", 1.60, 100, "2026-08-03", pre_close=1.45)
    assert not r.success and r.reason == "limit_up_sealed"
    assert b.buy("600000", 1.59, 100, "2026-08-03", pre_close=1.45).success


def test_is_st_name():
    assert is_st_name("*ST康美") and is_st_name("ST 中天") and not is_st_name("平安银行")
    assert not is_st_name(None)


def test_buy_at_limit_up_refused_sell_at_limit_down_refused():
    b = Broker(_cost())
    r = b.buy("600000", 11.0, 100, FRI, pre_close=10.0)
    assert not r.success and r.reason == "limit_up_sealed"
    assert b.buy("600000", 10.99, 100, FRI, pre_close=10.0).success
    r = b.sell("600000", 9.0, 100, MON, pre_close=10.0)
    assert not r.success and r.reason == "limit_down_sealed"
    assert b.sell("600000", 9.01, 100, MON, pre_close=10.0).success


def test_chinext_301_limit_and_st_limit():
    b = Broker(_cost())
    assert b.buy("301001", 11.5, 100, FRI, pre_close=10.0).success  # 20% 板, 11.5 未涨停
    r = b.buy("600000", 10.5, 100, "2026-07-03", pre_close=10.0, name="ST某某")  # 新规前 5%
    assert not r.success and r.reason == "limit_up_sealed"


# ---------------------------------------------------------------- broker: 日内亏损比例
def test_daily_loss_is_ratio_of_start_nav_small_loss_does_not_block():
    b = Broker(_cost(1_000_000))
    b.buy("600000", 10.0, 1000, FRI)
    b.sell("600000", 9.5, 1000, MON)  # 亏 ~500 元 << 5% NAV
    assert b.buy("000001", 10.0, 100, MON).success  # 旧实现: -500 < -0.05 → 误拦


def test_daily_loss_over_5pct_blocks_buys():
    b = Broker(_cost(100_000))
    b.buy("600000", 10.0, 9000, FRI)
    b.sell("600000", 9.2, 9000, MON)  # 亏 ~7200+费 ≈ -7.4% 开盘 NAV
    assert b.daily_loss_ratio(MON) < -0.05
    r = b.buy("000001", 10.0, 100, MON)
    assert not r.success and r.reason == "max_daily_loss_exceeded"


# ---------------------------------------------------------------- broker: 交易记录含买入佣金
def test_sell_pnl_net_of_buy_and_sell_costs():
    b = Broker(_cost())
    buy = b.buy("600000", 10.0, 100, FRI)
    r = b.sell("600000", 10.0, 100, MON)
    assert r.gross_pnl == pytest.approx(0.0)
    assert r.buy_commission == pytest.approx(buy.commission)
    expected = -(buy.commission + buy.transfer_fee + r.commission + r.stamp_tax + r.transfer_fee)
    assert r.pnl == pytest.approx(expected)
    # 现金流与净 pnl 一致
    assert b.cash - 1_000_000 == pytest.approx(r.pnl)


# ---------------------------------------------------------------- hold: T+1 开盘成交
# 资金留出费用余量: AllInSizer 不预留佣金, 整除时 insufficient_funds (既有行为)
def _hold(days, codes=("600000",), fill="next_open", rng=(FRI, WED), cap=1_004_990, mode_cls=HoldMode):
    dl = _DL(days)
    m = mode_cls(Broker(_cost(cap)), dl, None)
    rep = m.run(BacktestInput(mode="hold", codes=list(codes), date_range=rng, fill_price=fill,
                              cost=_cost(cap)))
    return rep


_DAYS = {
    FRI: [_k("600000", 10.0)],
    MON: [_k("600000", 10.5, open_=10.2)],
    TUE: [_k("600000", 10.8)],
    WED: [_k("600000", 11.0)],
}


def test_next_open_fills_at_t_plus_1_open():
    rep = _hold(_DAYS)
    t = rep.trade_log[0]
    assert t["entry_date"] == MON and t["entry_price"] == pytest.approx(10.2)
    assert rep.data_quality["fill_price"] == "next_open"


def test_same_close_legacy_reproduces_signal_day_close():
    rep = _hold(_DAYS, fill="same_close")
    t = rep.trade_log[0]
    assert t["entry_date"] == FRI and t["entry_price"] == pytest.approx(10.0)


def test_next_open_suspended_entry_skipped():
    days = dict(_DAYS)
    days[MON] = [_k("000001", 5.0)]  # 600000 次日停牌 (无 kline)
    rep = _hold(days)
    assert rep.total_trades == 0
    assert "600000" in rep.data_quality["entry_skipped"]


def test_next_open_limit_up_open_not_bought():
    days = dict(_DAYS)
    days[MON] = [_k("600000", 11.0, open_=11.0, pre_close=10.0)]  # 一字涨停
    rep = _hold(days)
    assert rep.total_trades == 0
    assert "600000" in rep.data_quality["entry_skipped"]


def test_pre_close_fallback_from_previous_day_close():
    days = dict(_DAYS)
    days[MON] = [_k("600000", 11.0, open_=11.0)]  # 无 pre_close 字段 → 取 FRI 收盘 10
    rep = _hold(days)
    assert rep.total_trades == 0


def test_nav_curve_only_trading_days():
    rep = _hold(_DAYS)
    dates = [e["date"] for e in rep.equity_curve]
    assert dates == [FRI, MON, TUE, WED]  # 周末不记点, 窗尾不重复
    assert len(dates) == len(set(dates))


# ---------------------------------------------------------------- portfolio: 收盘 NAV
def test_portfolio_final_nav_values_unsold_at_last_price():
    days = {
        FRI: [_k("600000", 10.0), _k("000001", 20.0)],
        MON: [_k("600000", 10.0), _k("000001", 20.0)],
        TUE: [_k("600000", 10.5), _k("000001", 21.0)],
        WED: [_k("600000", 11.0)],  # 000001 窗尾缺价 → 未卖出
    }
    rep = _hold(days, codes=("600000", "000001"), mode_cls=PortfolioMode)
    assert rep.total_trades == 1
    assert rep.sell_data_missing == ["000001"]
    # 旧实现 nav({}) 把 000001 (~50% 资金) 按 0 计 → ≈ -0.5
    assert rep.total_return > 0

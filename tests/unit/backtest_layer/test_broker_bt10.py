"""BT-10 Broker 补真测试 — 量能限制 / 订单历史 / T+1

验收: 三项真实 — ① 量能限制接真实 volume (缺失→限制不生效但标注);
② buy/sell 记 _order_history; ③ T+1 当日买入份额不可当日卖。
"""
from backtest_layer.core.broker import Broker
from backtest_layer.core.models import CostConfig


def _broker(capital=1_000_000):
    return Broker(CostConfig(initial_capital=capital))


# ── ① 量能限制 ──────────────────────────────────────────────

def test_volume_limit_caps_fill():
    """volume=10000 → max_fill = 5% = 500 股, 买入 1000 只成交 500"""
    b = _broker()
    result = b.buy("000001", 10.0, 1000, "2026-08-18", volume=10000)
    assert result.success
    assert result.shares == 500
    order = b.get_order_history("000001")[-1]
    assert order["volume_limited"] is True
    assert order["volume_known"] is True


def test_volume_unknown_no_limit():
    """volume 缺失 → 限制不生效, 全额成交, 订单历史标注 volume_known=False"""
    b = _broker()
    result = b.buy("000001", 10.0, 1000, "2026-08-18", volume=None)
    assert result.success
    assert result.shares == 1000
    order = b.get_order_history("000001")[-1]
    assert order["volume_known"] is False
    assert order["volume_limited"] is False


def test_volume_fill_too_small():
    """volume=1000 → max_fill = 50//100*100 = 0 → 拒单 volume_fill_too_small"""
    b = _broker()
    result = b.buy("000001", 10.0, 1000, "2026-08-18", volume=1000)
    assert not result.success
    assert result.reason == "volume_fill_too_small"


# ── ② 订单历史 ──────────────────────────────────────────────

def test_order_history_records_buy_and_sell():
    b = _broker()
    b.buy("000001", 10.0, 100, "2026-08-18")
    b.sell("000001", 11.0, 100, "2026-08-19")
    history = b.get_order_history("000001")
    assert len(history) == 2
    assert history[0]["action"] == "buy" and history[0]["success"] is True
    assert history[1]["action"] == "sell" and history[1]["success"] is True


def test_order_history_records_failures():
    """失败订单也记录 (原实现 _order_history 声明了但从不写入)"""
    b = _broker(capital=100)
    result = b.buy("000001", 10.0, 100, "2026-08-18")
    assert not result.success
    history = b.get_order_history()
    assert len(history) == 1
    assert history[0]["success"] is False
    assert history[0]["reason"] == "insufficient_funds"


# ── ③ T+1 ───────────────────────────────────────────────────

def test_t_plus_1_same_day_sell_blocked():
    """当日买入份额当日不可卖"""
    b = _broker()
    b.buy("000001", 10.0, 100, "2026-08-18")
    result = b.sell("000001", 10.5, 100, "2026-08-18")
    assert not result.success
    assert result.reason == "t_plus_1_locked"


def test_t_plus_1_next_day_sell_ok():
    """次日可卖"""
    b = _broker()
    b.buy("000001", 10.0, 100, "2026-08-18")
    result = b.sell("000001", 10.5, 100, "2026-08-19")
    assert result.success


def test_t_plus_1_partial_unlocked():
    """前日持仓可卖, 当日新增锁定"""
    b = _broker()
    b.buy("000001", 10.0, 100, "2026-08-17")   # 前日持仓
    b.buy("000001", 10.0, 100, "2026-08-18")   # 当日新增 → 锁定
    # 当日只能卖前日的 100 股
    result = b.sell("000001", 10.5, 100, "2026-08-18")
    assert result.success
    # 再卖 100 → 当日新增被锁
    result2 = b.sell("000001", 10.5, 100, "2026-08-18")
    assert not result2.success
    assert result2.reason == "t_plus_1_locked"

"""D4 — get_available_cash 现金双计 bug 回归测试 (TDD 红→绿)

bug: 原公式 现金 = 初始资金 − active在投 + exited回款, 漏扣 exited 股票的
入场成本 → 每笔已退出交易的成本被重复计入现金, NAV 虚增。
正确公式: 现金 = 初始资金 − active在投 − exited入场成本 + exited回款。

回归锚 = 2026-08-12 生产真实批次 (7 只建仓 08-15 全退):
入场成本合计 196,033, 回款合计 192,899 → 正确现金 296,866 (bug 公式给 492,899)。
"""
import pytest

from common.database import Database
from execution_layer.core.stock_pool import StockPool

INITIAL = 300000.0


@pytest.fixture
def pool():
    db = Database(":memory:")
    db.init_db()
    p = StockPool(db)
    p._ensure_table()
    yield p
    db.close()


def _insert(pool, code, status, entry_price, exit_price, qty):
    pool.db.get_connection().execute(
        "INSERT INTO stock_pool (code, name, status, entry_price, exit_price, position_qty, added_date) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-08-12')",
        (code, code, status, entry_price, exit_price, qty),
    )
    pool.db.get_connection().commit()


def test_no_positions_returns_initial(pool):
    assert pool.get_available_cash(INITIAL) == pytest.approx(INITIAL)


def test_exit_pending_keeps_cash_reserved_until_sale(pool):
    _insert(pool, "688029", "exit_pending", 72.29, None, 600)
    assert pool.get_available_cash(INITIAL) == pytest.approx(256626.0)


def test_bridge_includes_unsold_exit_pending_positions(pool):
    from backtest_layer.paper_trading.bridge import PaperTradingBridge
    _insert(pool, "600000", "active", 10.0, None, 1000)
    _insert(pool, "688029", "exit_pending", 72.29, None, 600)
    _insert(pool, "000001", "exited", 20.0, 21.0, 500)
    bridge = PaperTradingBridge(db=pool.db)
    assert {p.code for p in bridge.positions()} == {"600000", "688029"}
    assert bridge.cash() == pytest.approx(247126.0)
    # Explicit synthetic market prices: the pending position still contributes PnL.
    prices = {"600000": 11.0, "688029": 80.0}
    assert bridge.portfolio_value(prices) == pytest.approx(306126.0)
    assert bridge.unrealized_pnl(prices) == pytest.approx(5626.0)


def test_only_active_deducts_invested(pool):
    _insert(pool, "600000", "active", 10.0, None, 1000)
    assert pool.get_available_cash(INITIAL) == pytest.approx(INITIAL - 10000.0)


def test_exited_deducts_entry_cost_and_adds_proceeds(pool):
    """合成用例: 1 active + 1 exited, 锁住正确公式"""
    _insert(pool, "600000", "active", 10.0, None, 1000)      # 在投 10,000
    _insert(pool, "000001", "exited", 20.0, 21.0, 500)        # 成本 10,000 回款 10,500
    # 正确: 300,000 − 10,000 − 10,000 + 10,500 = 290,500
    # bug 公式: 300,000 − 10,000 + 10,500 = 300,500 (虚增成本 10,000)
    assert pool.get_available_cash(INITIAL) == pytest.approx(290500.0)


def test_regression_20260812_real_batch(pool):
    """生产真实数据回归: 08-12 建仓 7 只, 08-15 全部退出 (当前账本唯一实弹批次)"""
    batch = [
        # code, entry_price, exit_price, qty  (生产 stock_pool exited 行原值)
        ("001232", 170.7, 161.27, 200),
        ("600461", 8.96, 8.88, 4400),
        ("603202", 53.67, 52.27, 600),
        ("601881", 12.36, 12.21, 2300),
        ("000521", 4.95, 4.88, 5000),
        ("920189", 21.23, 20.95, 900),
        ("002543", 6.66, 6.99, 2700),
    ]
    for code, ep, xp, qty in batch:
        _insert(pool, code, "exited", ep, xp, qty)
    entry_cost = sum(ep * qty for _, ep, _, qty in batch)     # 196,033
    proceeds = sum(xp * qty for _, _, xp, qty in batch)       # 192,899
    assert entry_cost == pytest.approx(196033.0)
    assert proceeds == pytest.approx(192899.0)
    # 正确现金 = 300,000 − 196,033 + 192,899 = 296,866 (bug 公式给 492,899)
    assert pool.get_available_cash(INITIAL) == pytest.approx(296866.0)


def test_bridge_portfolio_value_not_inflated(pool):
    """bridge 现金同式 — 修后组合净值不再虚增 exited 成本"""
    from backtest_layer.paper_trading.bridge import PaperTradingBridge
    _insert(pool, "001232", "exited", 170.7, 161.27, 200)
    bridge = PaperTradingBridge(db=pool.db)
    # 无 active 持仓: 组合净值 == 现金 == 初始 − 成本 + 回款
    expected = INITIAL - 170.7 * 200 + 161.27 * 200
    assert bridge.cash() == pytest.approx(expected)
    assert bridge.portfolio_value() == pytest.approx(expected)

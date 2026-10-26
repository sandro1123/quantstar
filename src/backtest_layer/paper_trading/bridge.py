"""backtest_layer/paper_trading/bridge.py — PaperTradingBridge (D-24)

执行层唯一账本之上的纸交易只读视图。

D-24 裁定: PaperBroker 保留唯一生产纸账本。BT-12 纸交易不再持有影子账本 —
旧桥的 virtual_capital 内存资金线、自记账 positions/trades 全部退役;
成交/持仓/现金/盈亏一律读执行层 stock_pool / action_records (单一事实源)。
"""

from dataclasses import dataclass

from common.logging import get_logger

logger = get_logger("backtest.paper_trading")

DEFAULT_INITIAL_CAPITAL = 300000.0


@dataclass
class LedgerPosition:
    """从执行层账本 (stock_pool status=active/exit_pending) 读出的持仓"""
    code: str
    name: str = ""
    qty: int = 0
    entry_price: float = 0.0
    last_price: float = 0.0
    unrealized_pnl: float = 0.0


class PaperTradingBridge:
    """执行层唯一账本的只读视图 — 不自记账、无内存资金。

    资金基准统一 execution.initial_capital; 现金与执行层同式
    (StockPool.get_available_cash: 初始资金 − 在投 − exited入场成本 + 退出回款, D4 修后)。
    """

    def __init__(self, db=None, config=None):
        self.db = db
        self.config = config

    @property
    def initial_capital(self) -> float:
        cfg = self.config
        if cfg is not None and hasattr(cfg, "get"):
            try:
                return float(cfg.get("execution.initial_capital", DEFAULT_INITIAL_CAPITAL))
            except (TypeError, ValueError):
                pass
        return DEFAULT_INITIAL_CAPITAL

    def _stock_pool(self):
        if self.db is None:
            return None
        from execution_layer.core.stock_pool import StockPool
        return StockPool(self.db, self.config)

    def positions(self) -> list[LedgerPosition]:
        """活跃持仓 (读 stock_pool status=active/exit_pending); 无 db/无表 → 空 list, 不造假。"""
        pool = self._stock_pool()
        if pool is None:
            return []
        try:
            rows = pool.get_by_status("active") + pool.get_by_status("exit_pending")
        except Exception:
            return []
        return [
            LedgerPosition(
                code=r.get("code", ""), name=r.get("name") or "",
                qty=r.get("position_qty") or 0,
                entry_price=r.get("entry_price") or 0.0,
                last_price=r.get("last_price") or 0.0,
                unrealized_pnl=r.get("unrealized_pnl") or 0.0,
            )
            for r in rows if r.get("code")
        ]

    def cash(self) -> float:
        """可用现金 — 与执行层同式 (get_available_cash); 读不到 → 初始资金。"""
        pool = self._stock_pool()
        if pool is None:
            return self.initial_capital
        try:
            return pool.get_available_cash(self.initial_capital)
        except Exception:
            return self.initial_capital

    def portfolio_value(self, current_prices: dict[str, float] | None = None) -> float:
        """组合市值 = 现金 + 持仓市值 (市价优先 current_prices, 回退账本 last_price)。"""
        value = self.cash()
        for pos in self.positions():
            price = (current_prices or {}).get(pos.code) or pos.last_price or pos.entry_price
            value += price * pos.qty
        return value

    def unrealized_pnl(self, current_prices: dict[str, float] | None = None) -> float:
        """未实现盈亏 = Σ(市价 − 入场价) × 数量, 持仓读自账本。"""
        total = 0.0
        for pos in self.positions():
            price = (current_prices or {}).get(pos.code) or pos.last_price or pos.entry_price
            total += (price - pos.entry_price) * pos.qty
        return total

    def realized_pnl(self) -> float:
        """已实现盈亏 — 账本 exited 行 (入场成本 × total_return_pct)。"""
        pool = self._stock_pool()
        if pool is None:
            return 0.0
        try:
            rows = pool.get_by_status("exited")
        except Exception:
            return 0.0
        total = 0.0
        for r in rows:
            cost = (r.get("entry_price") or 0.0) * (r.get("position_qty") or 0)
            total += cost * (r.get("total_return_pct") or 0.0) / 100.0
        return total

    def fills(self, date: str | None = None) -> list[dict]:
        """成交记录 — 读 action_records entry/exit (可选按日期过滤)。"""
        if self.db is None:
            return []
        try:
            conn = self.db.get_connection()
            if date:
                rows = conn.execute(
                    "SELECT * FROM action_records "
                    "WHERE action_type IN ('entry', 'exit') AND created_at LIKE ? "
                    "ORDER BY created_at", (f"{date}%",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM action_records "
                    "WHERE action_type IN ('entry', 'exit') ORDER BY created_at",
                ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def summary(self) -> dict:
        """账本快照摘要 (只读)。"""
        cash = self.cash()
        nav = self.portfolio_value()
        initial = self.initial_capital
        return {
            "initial_capital": initial,
            "cash": round(cash, 2),
            "positions": len(self.positions()),
            "portfolio_value": round(nav, 2),
            "unrealized_pnl": round(self.unrealized_pnl(), 2),
            "realized_pnl": round(self.realized_pnl(), 2),
            "return_pct": round((nav / initial - 1) * 100, 2) if initial else 0.0,
        }

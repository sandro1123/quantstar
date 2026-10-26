"""Pre-execution constraint checks (G-E31) + fee calculation (G-E19)"""

from dataclasses import dataclass


def calculate_fees(price: float, quantity: int, is_buy: bool = True) -> dict:
    """Calculate A-share transaction fees"""
    amount = price * quantity
    commission = max(amount * 0.00025, 5.0)
    stamp_tax = 0 if is_buy else amount * 0.0005
    transfer_fee = amount * 0.00002
    total = commission + stamp_tax + transfer_fee
    return {
        "commission": round(commission, 2),
        "stamp_tax": round(stamp_tax, 2),
        "transfer_fee": round(transfer_fee, 2),
        "total": round(total, 2),
        "net_amount": round(amount - total, 2),
    }


@dataclass
class ConstraintResult:
    passed: bool
    violations: list[str] = None

    def __post_init__(self):
        if self.violations is None:
            self.violations = []


class ConstraintChecker:
    def __init__(self, stock_pool, total_capital: float = 300000.0, config=None):
        self.stock_pool = stock_pool
        cfg_get = (config.get if hasattr(config, 'get') else lambda k, d=None: d) if config else (lambda k, d=None: d)
        self.initial_capital = cfg_get("execution.initial_capital", total_capital)
        self.max_positions = cfg_get("execution.max_positions", 10)
        self.max_single_stock_pct = cfg_get("execution.max_single_stock_pct", 20.0)

    def _get_available_cash(self) -> float:
        """动态计算可用现金，优先用 StockPool.get_available_cash，fallback 到直接计算"""
        if hasattr(self.stock_pool, 'get_available_cash') and callable(self.stock_pool.get_available_cash):
            try:
                result = self.stock_pool.get_available_cash(self.initial_capital)
                if isinstance(result, (int, float)):
                    return result
            except Exception:
                pass
        active = self.stock_pool.get_by_status("active")
        total_invested = sum(
            (p.get("entry_price", 0) or 0) * (p.get("position_qty", 0) or 0)
            for p in active
        )
        return self.initial_capital - total_invested

    def check_entry(self, code: str, current_price: float, position_qty: int = 100) -> ConstraintResult:
        """Check all entry constraints before executing buy"""
        violations = []

        # 1. T+1 check: stock already bought today?
        pool = self.stock_pool.get(code)
        if pool and pool.get("entry_date"):
            from common.utils import bj_date
            if pool["entry_date"][:10] == bj_date():
                violations.append("T+1限制: 今日已买入，不能重复买入")

        # 2. Minimum 100 shares (A-share lot size)
        if position_qty < 100:
            violations.append(f"最小交易单位: 需至少100股 (当前请求: {position_qty}股)")

        # 3. Cash balance check (dynamic)
        cash_remaining = self._get_available_cash()
        cost = current_price * position_qty
        if cost > cash_remaining:
            violations.append(f"余额不足: 需{cost:.2f}元，可用{cash_remaining:.2f}元")

        # 4. Max position count
        active = self.stock_pool.get_by_status("active")
        if len(active) >= self.max_positions:
            violations.append(f"持仓数量已达上限{self.max_positions}只")

        # 5. Max single position
        new_position_value = current_price * position_qty
        total_invested = sum(
            (p.get("entry_price", 0) or 0) * (p.get("position_qty", 0) or 0)
            for p in active
        )
        current_pct = (total_invested + new_position_value) / self.initial_capital * 100
        if current_pct > self.max_single_stock_pct:
            violations.append(f"单只仓位上限{self.max_single_stock_pct:.0f}%，当前: {current_pct:.1f}%")

        return ConstraintResult(passed=len(violations) == 0, violations=violations)

    def check_exit(self, code: str) -> ConstraintResult:
        """Check exit constraints"""
        violations = []
        pool = self.stock_pool.get(code)
        if not pool:
            violations.append("未持仓")
            return ConstraintResult(passed=False, violations=violations)
        if pool.get("status") != "active":
            violations.append(f"状态异常: {pool.get('status')}，无法卖出")
        # T+1 check
        if pool.get("entry_date"):
            from common.utils import bj_date
            if pool["entry_date"][:10] == bj_date():
                violations.append("T+1限制: 今日买入，明日才能卖出")
        return ConstraintResult(passed=len(violations) == 0, violations=violations)

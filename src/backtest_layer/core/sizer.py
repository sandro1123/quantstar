"""Auto Sizer — 仓位计算器"""


class PercentSizer:
    def __init__(self, risk_per_trade: float = 0.02, max_position_pct: float = 0.25):
        self.risk_per_trade = risk_per_trade
        self.max_position_pct = max_position_pct

    def size(self, capital: float, price: float, num_positions: int = 1) -> int:
        if price <= 0 or capital <= 0:
            return 0
        position_capital = capital * self.max_position_pct
        target = min(position_capital, capital * self.risk_per_trade * num_positions)
        shares = int(target / price / 100) * 100
        return max(0, shares)


class AllInSizer:
    def size(self, capital: float, price: float, num_positions: int = 1) -> int:
        if price <= 0 or capital <= 0:
            return 0
        per_stock = capital / max(1, num_positions)
        shares = int(per_stock / price / 100) * 100
        return max(0, shares)


class FixedSizer:
    def __init__(self, shares: int = 1000):
        self.shares = shares

    def size(self, capital: float, price: float, num_positions: int = 1) -> int:
        return self.shares

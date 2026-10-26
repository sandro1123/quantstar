"""再平衡引擎 — 加仓/减仓/调仓/分阶段退出 + 市场状态感知"""
from dataclasses import dataclass

from common.database import Database
from common.logging import get_logger

logger = get_logger("execution.rebalance")

_REGIME_MAP = {
    "bull": {"max_positions_pct": 1.0, "max_single_pct": 1.0, "label": "牛市 满仓"},
    "oscillating": {"max_positions_pct": 0.6, "max_single_pct": 0.7, "label": "震荡 六成"},
    "bear": {"max_positions_pct": 0.3, "max_single_pct": 0.4, "label": "熊市 三成"},
}


@dataclass
class AddCheckResult:
    can_add: bool = False
    reason: str = ""
    max_add_qty: int = 0
    current_pct: float = 0.0
    regime: str = ""


@dataclass
class ReduceCheckResult:
    can_reduce: bool = False
    reason: str = ""
    reduce_qty: int = 0
    current_pct: float = 0.0
    regime: str = ""


@dataclass
class SwapCheckResult:
    can_swap: bool = False
    reason: str = ""
    regime: str = ""


class RebalanceEngine:
    def __init__(self, db: Database, stock_pool, total_capital: float = 300000.0, config=None):
        self.db = db
        self.stock_pool = stock_pool
        cfg_get = (config.get if hasattr(config, 'get') else lambda k, d=None: d) if config else (lambda k, d=None: d)
        self.total_capital = cfg_get("execution.total_capital", total_capital)
        self.max_positions = cfg_get("execution.max_positions", 10)
        self.max_single_stock_pct = cfg_get("execution.max_single_stock_pct", 20.0)
        self._current_regime = "oscillating"

    def set_regime(self, regime: str):
        if regime in _REGIME_MAP:
            self._current_regime = regime
            logger.info(f"Rebalance regime set to {regime}")

    def get_risk_degree(self) -> float:
        """返回 0-1 风险度，1=满仓，0=空仓"""
        mapping = _REGIME_MAP.get(self._current_regime, _REGIME_MAP["oscillating"])
        return mapping["max_positions_pct"]

    def get_regime_label(self) -> str:
        mapping = _REGIME_MAP.get(self._current_regime, _REGIME_MAP["oscillating"])
        return mapping["label"]

    def _effective_max_positions(self) -> int:
        return max(1, int(self.max_positions * self.get_risk_degree()))

    def _effective_max_single_pct(self) -> float:
        mapping = _REGIME_MAP.get(self._current_regime, _REGIME_MAP["oscillating"])
        return self.max_single_stock_pct * mapping["max_single_pct"]

    def check_add(self, code: str, current_price: float, total_capital: float) -> AddCheckResult:
        active = self.stock_pool.get_by_status("active")
        current = self.stock_pool.get(code)
        current_qty = current.get("position_qty", 0) if current else 0
        current_value = current_qty * current_price
        current_pct = current_value / total_capital * 100 if total_capital > 0 else 0
        effective_max_pct = self._effective_max_single_pct()
        effective_max_pos = self._effective_max_positions()

        if current_pct >= effective_max_pct:
            return AddCheckResult(can_add=False, reason=f"已达仓位上限 {effective_max_pct:.0f}% (当前市场: {self.get_regime_label()})")
        if len(active) >= effective_max_pos:
            return AddCheckResult(can_add=False, reason=f"持仓数量已达上限 {effective_max_pos} 只 (当前市场: {self.get_regime_label()})")
        remaining = effective_max_pct - current_pct
        max_add = remaining * total_capital / 100 / current_price
        return AddCheckResult(can_add=True, max_add_qty=int(max_add), current_pct=round(current_pct, 1), regime=self.get_regime_label())

    def check_reduce(self, code: str, current_price: float, total_capital: float) -> ReduceCheckResult:
        current = self.stock_pool.get(code)
        if not current:
            return ReduceCheckResult(can_reduce=False, reason="未持仓")
        qty = current.get("position_qty", 0) or 0
        value = qty * current_price
        pct = value / total_capital * 100 if total_capital > 0 else 100
        effective_max_pct = self._effective_max_single_pct()
        if pct > effective_max_pct:
            target_qty = int(effective_max_pct * total_capital / 100 / current_price)
            reduce_qty = max(0, qty - target_qty)
            # A 股卖出须为 100 股整数倍 (仅清仓时允许零股): 向上取整到整手, 不超过持仓
            if 0 < reduce_qty < qty:
                reduce_qty = min(qty, -(-reduce_qty // 100) * 100)
            return ReduceCheckResult(can_reduce=True, reduce_qty=reduce_qty, current_pct=round(pct, 1), regime=self.get_regime_label())
        return ReduceCheckResult(can_reduce=False, reason="仓位正常")

    def check_swap(self, old_code: str, new_code: str, current_price: float, total_capital: float) -> SwapCheckResult:
        old = self.stock_pool.get(old_code)
        if not old:
            return SwapCheckResult(can_swap=False, reason="旧股票未持仓")
        old_score = old.get("current_score", 0) or 0
        if old_score > 0.3:
            return SwapCheckResult(can_swap=False, reason=f"旧股票评分 {old_score:.2f} 仍较高")
        return SwapCheckResult(can_swap=True, reason="值得调仓", regime=self.get_regime_label())

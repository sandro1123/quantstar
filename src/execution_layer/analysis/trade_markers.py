"""Generate trade marker data for chart integration (G-E11)"""

from dataclasses import dataclass, field


@dataclass
class TradeMarker:
    code: str
    date: str
    marker_type: str
    price: float
    reason: str = ""


@dataclass
class TradeMarkerSet:
    code: str
    markers: list = field(default_factory=list)


class TradeMarkerGenerator:
    def __init__(self, stock_pool, action_record):
        self.stock_pool = stock_pool
        self.action_record = action_record

    def get_markers(self, code: str) -> TradeMarkerSet:
        pool = self.stock_pool.get(code)
        if not pool:
            return TradeMarkerSet(code=code, markers=[])
        markers = []
        if pool.get("entry_date") and pool.get("entry_price"):
            markers.append(TradeMarker(
                code=code, date=pool["entry_date"][:10],
                marker_type="entry", price=pool["entry_price"],
                reason=pool.get("entry_reason", ""),
            ))
        if pool.get("stop_loss"):
            markers.append(TradeMarker(
                code=code, date=pool.get("entry_date", "")[:10],
                marker_type="stop_loss", price=pool["stop_loss"],
                reason="止损线",
            ))
        if pool.get("take_profit"):
            markers.append(TradeMarker(
                code=code, date=pool.get("entry_date", "")[:10],
                marker_type="take_profit", price=pool["take_profit"],
                reason="止盈线",
            ))
        if pool.get("exit_date") and pool.get("exit_price"):
            markers.append(TradeMarker(
                code=code, date=pool["exit_date"][:10],
                marker_type="exit", price=pool["exit_price"],
                reason=pool.get("exit_reason", ""),
            ))
        return TradeMarkerSet(code=code, markers=sorted(markers, key=lambda m: m.date))

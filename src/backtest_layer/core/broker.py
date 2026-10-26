"""backtest_layer/core/broker.py — 交易模拟器（含涨跌停限价 + 多订单类型）"""

from dataclasses import dataclass

from backtest_layer.core.models import CostConfig


@dataclass
class TradeResult:
    success: bool = False
    reason: str = ""
    fill_price: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    pnl: float = 0.0
    order_type: str = ""
    shares: int = 0
    # 卖出: pnl = 净盈亏 (扣买入佣金/过户费按股分摊 + 卖出佣金/印花税/过户费);
    # gross_pnl = (成交价 - 均价) * 股数 (费前); buy_commission = 分摊到本次卖出股数的买入佣金
    gross_pnl: float = 0.0
    buy_commission: float = 0.0
    buy_transfer_fee: float = 0.0


def is_st_name(name: str | None) -> bool:
    """名称含 ST / *ST 视为风险警示股 (±5%)。名称缺失 → False (无法判定, 按板块口径)。"""
    if not name:
        return False
    n = str(name).upper().replace(" ", "")
    return n.startswith("ST") or n.startswith("*ST") or n.startswith("S*ST")


def _get_price_limit_pct(code: str, name: str | None = None, is_st: bool = False) -> float:
    """根据代码 (+名称/ST 标记) 判断涨跌停幅度
    - 300xxx/301xxx (创业板) / 688xxx/689xxx (科创板): ±20%
    - 北交所 (8xxxxx / 4xxxxx / 92xxxx): ±30%
    - ST/*ST (主板): ±5% — 由 is_st 或 name 判定 (创业板/科创板/北交所 ST 仍按板块幅度)
    - 其余主板: ±10%
    注: ST 状态为调用时点名称口径, 非 point-in-time (见 PR TODO)。
    """
    code = str(code or "")
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    if is_st or is_st_name(name):
        return 0.05
    return 0.10


class Broker:
    A_SHARE_LOT = 100

    def __init__(self, cost: CostConfig):
        self.cost = cost
        self.cash = cost.initial_capital
        self.positions: dict[str, dict] = {}
        self._daily_realized_pnl: dict[str, float] = {}
        # 日内已实现亏损阈值 = 占当日开盘 NAV 的比例 (原实现拿元直接比 -0.05, 单位错配)
        self._max_daily_loss = -0.05
        self._day_start_nav: dict[str, float] = {}
        self._daily_high: dict[str, float] = {}
        self._trail_pct = 0.05
        self._order_history: list[dict] = []
        self._volume_limit_pct = 0.05  # 单笔成交不超过日成交量5%

    def _calc_price_limit(self, code: str, pre_close: float, name: str | None = None,
                          is_st: bool = False) -> tuple[float, float]:
        limit_pct = _get_price_limit_pct(code, name=name, is_st=is_st)
        # 交易所口径: 涨跌停价四舍五入到分
        return round(pre_close * (1 - limit_pct), 2), round(pre_close * (1 + limit_pct), 2)

    def validate_order(self, code: str, price: float, shares: int, pre_close: float | None = None,
                       side: str | None = None, name: str | None = None,
                       is_st: bool = False) -> TradeResult | None:
        if not code or len(code) < 6:
            return TradeResult(success=False, reason="invalid_code")
        if price <= 0:
            return TradeResult(success=False, reason="invalid_price")
        if shares <= 0:
            return TradeResult(success=False, reason="invalid_shares")
        if shares % self.A_SHARE_LOT != 0:
            return TradeResult(success=False, reason="not_round_lot")
        if pre_close is not None and pre_close > 0:
            limit_down, limit_up = self._calc_price_limit(code, pre_close, name=name, is_st=is_st)
            eps = 0.005  # 半分容差 (价格精度到分)
            if price > limit_up + eps:
                return TradeResult(success=False, reason="price_above_limit_up")
            if price < limit_down - eps:
                return TradeResult(success=False, reason="price_below_limit_down")
            # 封板: 涨停价买不进, 跌停价卖不出 (保守按封死处理)
            if side == "buy" and price >= limit_up - eps:
                return TradeResult(success=False, reason="limit_up_sealed")
            if side == "sell" and price <= limit_down + eps:
                return TradeResult(success=False, reason="limit_down_sealed")
        return None

    def _position_cost_value(self) -> float:
        return sum(p["avg_price"] * p["shares"] for p in self.positions.values())

    def mark_day_start(self, date: str, prices: dict[str, float] | None = None) -> float:
        """记录当日开盘前 NAV (日内亏损比例的分母)。已记录则不覆盖。

        prices 缺省/缺票时按持仓成本价估值 (broker 不持有行情)。
        """
        if date not in self._day_start_nav:
            prices = prices or {}
            pos_val = sum(prices.get(c, p["avg_price"]) * p["shares"] for c, p in self.positions.items())
            self._day_start_nav[date] = self.cash + pos_val
        return self._day_start_nav[date]

    def daily_loss_ratio(self, date: str) -> float:
        """当日已实现盈亏 / 当日开盘 NAV (负数=亏损)。"""
        pnl = self._daily_realized_pnl.get(date, 0.0)
        base = self._day_start_nav.get(date) or self.mark_day_start(date)
        return pnl / base if base > 0 else 0.0

    def buy(self, code: str, price: float, shares: int, date: str,
            order_type: str = "market", pre_close: float | None = None,
            stop_price: float | None = None, limit_price: float | None = None,
            volume: float | None = None, name: str | None = None,
            is_st: bool = False) -> TradeResult:
        self.mark_day_start(date)
        validation = self.validate_order(code, price, shares, pre_close, side="buy",
                                         name=name, is_st=is_st)
        if validation:
            self._record_order(code, "buy", date, price, shares, order_type,
                               success=False, reason=validation.reason)
            return validation
        if self.daily_loss_ratio(date) < self._max_daily_loss:
            self._record_order(code, "buy", date, price, shares, order_type,
                               success=False, reason="max_daily_loss_exceeded")
            return TradeResult(success=False, reason="max_daily_loss_exceeded")
        slippage = price * self.cost.slippage_rate
        if order_type == "stop" or order_type == "limit":
            actual_price = price
        elif order_type == "stop_limit":
            if stop_price is not None and price >= stop_price:
                actual_price = limit_price if limit_price else price
            else:
                self._record_order(code, "buy", date, price, shares, order_type,
                                   success=False, reason="stop_not_triggered")
                return TradeResult(success=False, reason="stop_not_triggered")
        elif order_type == "stop_trail":
            high = max(self._daily_high.get(code, price), price)
            self._daily_high[code] = high
            trail_stop = high * (1 - self._trail_pct)
            if price <= trail_stop:
                actual_price = price
            else:
                self._record_order(code, "buy", date, price, shares, order_type,
                                   success=False, reason="trail_not_triggered")
                return TradeResult(success=False, reason="trail_not_triggered")
        else:
            actual_price = price + slippage
        commission = max(actual_price * shares * self.cost.commission_rate, self.cost.min_commission)
        transfer_fee = actual_price * shares * self.cost.transfer_fee_rate
        total_cost = actual_price * shares + commission + transfer_fee
        if total_cost > self.cash:
            self._record_order(code, "buy", date, price, shares, order_type,
                               success=False, reason="insufficient_funds")
            return TradeResult(success=False, reason="insufficient_funds")
        # BT-10: 量能限制接真实 volume — 调用方从 kline 读入当日成交量。
        # volume 缺失 (None/0) 时限制不生效但在订单历史标注 volume_unknown,
        # 不再硬编码 daily_volume=0 假装限制存在 (原实现 max_fill 恒等于 shares)。
        volume_limited = False
        if volume is not None and volume > 0 and self._volume_limit_pct > 0:
            max_fill = int(volume * self._volume_limit_pct) // 100 * 100
            if max_fill < shares:
                shares = max_fill
                volume_limited = True
                if shares <= 0:
                    self._record_order(code, "buy", date, price, shares, order_type,
                                       success=False, reason="volume_fill_too_small",
                                       volume_known=True)
                    return TradeResult(success=False, reason="volume_fill_too_small")
                commission = max(actual_price * shares * self.cost.commission_rate, self.cost.min_commission)
                transfer_fee = actual_price * shares * self.cost.transfer_fee_rate
                total_cost = actual_price * shares + commission + transfer_fee
                if total_cost > self.cash:
                    self._record_order(code, "buy", date, price, shares, order_type,
                                       success=False, reason="insufficient_funds",
                                       volume_known=True)
                    return TradeResult(success=False, reason="insufficient_funds")
        self.cash -= total_cost
        if code in self.positions:
            old = self.positions[code]
            total_shares = old["shares"] + shares
            avg_price = (old["avg_price"] * old["shares"] + actual_price * shares) / total_shares
            self.positions[code] = {"shares": total_shares, "avg_price": avg_price,
                                    "lock_date": old.get("lock_date"),
                                    "locked_shares": old.get("locked_shares", 0),
                                    "buy_commission": old.get("buy_commission", 0.0) + commission,
                                    "buy_transfer_fee": old.get("buy_transfer_fee", 0.0) + transfer_fee}
        else:
            self.positions[code] = {"shares": shares, "avg_price": actual_price,
                                    "lock_date": None, "locked_shares": 0,
                                    "buy_commission": commission,
                                    "buy_transfer_fee": transfer_fee}
        # BT-10 T+1: 当日买入份额锁定, 不可当日卖出
        pos = self.positions[code]
        if pos.get("lock_date") == date:
            pos["locked_shares"] = pos.get("locked_shares", 0) + shares
        else:
            pos["lock_date"] = date
            pos["locked_shares"] = shares
        self._record_order(code, "buy", date, actual_price, shares, order_type,
                           success=True, reason="", volume_known=volume is not None and volume > 0,
                           volume_limited=volume_limited)
        return TradeResult(success=True, fill_price=actual_price, commission=commission, transfer_fee=transfer_fee, order_type=order_type, shares=shares)

    def sell(self, code: str, price: float, shares: int, date: str,
             order_type: str = "market", pre_close: float | None = None,
             name: str | None = None, is_st: bool = False) -> TradeResult:
        self.mark_day_start(date)
        if code not in self.positions or self.positions[code]["shares"] < shares:
            self._record_order(code, "sell", date, price, shares, order_type,
                               success=False, reason="insufficient_shares")
            return TradeResult(success=False, reason="insufficient_shares")
        # BT-10 T+1: 当日买入的份额 (locked_shares) 不可当日卖出
        pos = self.positions[code]
        locked = pos.get("locked_shares", 0) if pos.get("lock_date") == date else 0
        available = pos["shares"] - locked
        if shares > available:
            self._record_order(code, "sell", date, price, shares, order_type,
                               success=False, reason="t_plus_1_locked")
            return TradeResult(success=False, reason="t_plus_1_locked")
        validation = self.validate_order(code, price, shares, pre_close, side="sell",
                                         name=name, is_st=is_st)
        if validation:
            self._record_order(code, "sell", date, price, shares, order_type,
                               success=False, reason=validation.reason)
            return validation
        slippage = price * self.cost.slippage_rate
        if order_type == "stop" or order_type == "limit":
            actual_price = price
        else:
            actual_price = price - slippage
        commission = max(actual_price * shares * self.cost.commission_rate, self.cost.min_commission)
        stamp_tax = actual_price * shares * self.cost.stamp_tax_rate
        transfer_fee = actual_price * shares * self.cost.transfer_fee_rate
        proceeds = actual_price * shares - commission - stamp_tax - transfer_fee
        pos = self.positions[code]
        avg_price = pos["avg_price"]
        # 买入费用按卖出股数比例分摊 (含买入佣金 — 交易记录口径与现金流一致)
        frac = shares / pos["shares"] if pos["shares"] > 0 else 0.0
        buy_comm = pos.get("buy_commission", 0.0) * frac
        buy_tf = pos.get("buy_transfer_fee", 0.0) * frac
        pos["buy_commission"] = pos.get("buy_commission", 0.0) - buy_comm
        pos["buy_transfer_fee"] = pos.get("buy_transfer_fee", 0.0) - buy_tf
        self.cash += proceeds
        pos["shares"] -= shares
        gross_pnl = (actual_price - avg_price) * shares
        pnl = gross_pnl - buy_comm - buy_tf - commission - stamp_tax - transfer_fee
        self._daily_realized_pnl[date] = self._daily_realized_pnl.get(date, 0) + pnl
        if self.positions[code]["shares"] == 0:
            del self.positions[code]
        self._record_order(code, "sell", date, actual_price, shares, order_type,
                           success=True, reason="")
        return TradeResult(success=True, fill_price=actual_price, commission=commission,
                          stamp_tax=stamp_tax, transfer_fee=transfer_fee, pnl=pnl, order_type=order_type, shares=shares,
                          gross_pnl=gross_pnl, buy_commission=buy_comm, buy_transfer_fee=buy_tf)

    def _record_order(self, code: str, action: str, date: str, price: float,
                      shares: int, order_type: str, success: bool, reason: str,
                      volume_known: bool | None = None, volume_limited: bool = False):
        """BT-10: buy/sell 记 _order_history (原字段声明了但从不写入)。

        volume_known=None 表示该订单不涉及量能判断 (sell); False 表示买入时
        量能未知 → 量能限制未生效 (显式标注, 不假装限制存在)。
        """
        self._order_history.append({
            "code": code, "action": action, "date": date, "price": price,
            "shares": shares, "order_type": order_type, "success": success,
            "reason": reason, "volume_known": volume_known,
            "volume_limited": volume_limited,
        })

    def apply_dividend(self, code: str, dividend_per_share: float, date: str):
        """模拟分红: 按持仓比例增加现金"""
        if code in self.positions:
            shares = self.positions[code]["shares"]
            total_dividend = shares * dividend_per_share
            self.cash += total_dividend
            self._daily_realized_pnl[date] = self._daily_realized_pnl.get(date, 0) + total_dividend
            return total_dividend
        return 0.0

    def get_order_history(self, code: str | None = None) -> list[dict]:
        if code:
            return [o for o in self._order_history if o["code"] == code]
        return self._order_history

    def nav(self, prices: dict[str, float]) -> float:
        position_value = sum(
            prices.get(code, 0) * pos["shares"]
            for code, pos in self.positions.items()
        )
        return self.cash + position_value

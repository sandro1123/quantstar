"""Signal Mode — 按 TradeSignal 序列执行（含事后剔除分析）

BT-11 ④ (B-11 语义修正): 原 n_drop 自称"撤单模拟", 实际是成交后删掉最差的
n 笔卖出交易重算 NAV — 这是改写历史不是模拟撤单。已改名 n_exclude 并标注
"事后剔除分析" (敏感性分析用途: 看剔除最差 N 笔后绩效如何)。
真撤单模拟 (限价单未成交即撤) 属从零写的大工程, 按 D-18 排队。
"""
from backtest_layer.core.broker import Broker
from common.logging import get_logger

logger = get_logger("backtest.signal")


def _normalize_series(raw) -> dict[str, float] | None:
    """把一只股票的价格数据规整成 {date: price}。

    支持 {date: price} 或 [{"date": ..., "close": ...}, ...]。
    纯 float 列表没有日期, 无法按信号日取价 → 返回 None (调用方只能用信号自带价格)。
    """
    if isinstance(raw, dict):
        return {str(d)[:10]: float(p) for d, p in raw.items() if p is not None}
    if isinstance(raw, (list, tuple)) and raw and isinstance(raw[0], dict):
        out = {}
        for row in raw:
            d = row.get("date")
            p = row.get("close", row.get("price"))
            if d is not None and p is not None:
                out[str(d)[:10]] = float(p)
        return out
    return None


def _price_on(series: dict[str, float] | None, date: str) -> tuple[float | None, float | None]:
    """返回 (信号日价格, 前一交易日价格)。信号日无价 → (None, None), 不向后取价 (防前视)。"""
    if not series or not date or date not in series:
        return None, None
    prev_dates = [d for d in series if d < date]
    pre_close = series[max(prev_dates)] if prev_dates else None
    return series[date], pre_close


def _last_known(series: dict[str, float] | None, date: str) -> float | None:
    if not series:
        return None
    past = [d for d in series if d <= date]
    return series[max(past)] if past else None


class SignalMode:
    def __init__(self, broker: Broker):
        self._broker = broker

    def run(self, signals: list[dict], price_data: dict, n_exclude: int = 0) -> dict:
        """按信号日价格成交。

        修复前: 所有信号都用整段序列最后一个价格成交 (前视 + 价格错误)。
        现在: 每条信号用信号日当天价格; price_data 无日期 (纯 float 列表) 时只接受
        信号自带的 price 字段, 否则跳过该信号并告警, 绝不拿未来价格成交。
        """
        equity_curve = []
        trade_count = 0
        winning_trades = 0
        trade_log = []
        skipped = 0
        series_map = {c: _normalize_series(raw) for c, raw in price_data.items()}
        last_seen: dict[str, float] = {}
        for signal in sorted(signals, key=lambda s: str(s.get("date", ""))):
            code = signal.get("code", "")
            action = signal.get("signal", "hold")
            date = str(signal.get("date", ""))[:10]
            series = series_map.get(code)
            current_price, pre_close = _price_on(series, date)
            if current_price is None and signal.get("price") is not None:
                current_price = float(signal["price"])
                pre_close = signal.get("pre_close")
            if current_price is None or current_price <= 0:
                if action in ("buy", "sell"):
                    skipped += 1
                    logger.warning(f"signal 模式: {code} {date} 无当日价格, 跳过 (不使用未来价格)")
                continue
            last_seen[code] = current_price
            name = signal.get("name")
            if action == "buy":
                shares = int(self._broker.cash * 0.9 / current_price / 100) * 100
                if shares > 0:
                    result = self._broker.buy(code, current_price, shares, date,
                                              pre_close=pre_close, name=name)
                    if result.success:
                        trade_count += 1
                        trade_log.append({
                            "code": code, "action": "buy", "date": date,
                            "price": result.fill_price, "shares": shares,
                        })
            elif action == "sell":
                pos = self._broker.positions.get(code)
                if pos and pos["shares"] > 0:
                    sell_shares = pos["shares"]  # 卖出前记录 (broker 会就地扣减)
                    result = self._broker.sell(code, current_price, sell_shares, date,
                                               pre_close=pre_close, name=name)
                    if result.success:
                        trade_count += 1
                        winning_trades += 1 if result.pnl > 0 else 0
                        trade_log.append({
                            "code": code, "action": "sell", "date": date,
                            "price": result.fill_price, "shares": sell_shares,
                            "pnl": result.pnl,
                        })
            marks = {}
            for c, ser in series_map.items():
                px = _last_known(ser, date) if ser else None
                if px is None:
                    px = last_seen.get(c)
                if px is not None:
                    marks[c] = px
            equity_curve.append(self._broker.nav(marks))

        final_nav = equity_curve[-1] if equity_curve else self._broker.cash

        # 事后剔除分析 (BT-11 ④): 剔除最差的 n_exclude 笔卖出交易重算 NAV —
        # 敏感性分析用途, 不是撤单模拟 (真撤单模拟排队)
        if n_exclude > 0 and trade_log:
            sell_trades = [t for t in trade_log if t["action"] == "sell" and "pnl" in t]
            if sell_trades:
                sell_trades.sort(key=lambda t: t.get("pnl", 0))
                dropped = sell_trades[:n_exclude]
                kept_pnl = sum(t.get("pnl", 0) for t in sell_trades) - sum(t.get("pnl", 0) for t in dropped)
                adjusted_nav = self._broker.cost.initial_capital + kept_pnl
                trade_count = max(0, trade_count - len(dropped))
                return {
                    "mode": "signal",
                    "final_nav": max(adjusted_nav, 0),
                    "equity_curve": [self._broker.cost.initial_capital, max(adjusted_nav, 0)],
                    "trade_count": trade_count,
                    "winning_trades": max(0, winning_trades - len([t for t in dropped if t.get("pnl", 0) > 0])),
                    "trade_log": trade_log,
                    "n_exclude": n_exclude,
                    "excluded_trades": len(dropped),
                }

        return {
            "mode": "signal",
            "final_nav": final_nav,
            "equity_curve": equity_curve,
            "trade_count": trade_count,
            "winning_trades": winning_trades,
            "trade_log": trade_log,
            "n_exclude": n_exclude,
            "skipped_signals": skipped,
        }

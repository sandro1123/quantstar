"""Signal Mode — 按 TradeSignal 序列执行（含事后剔除分析）

BT-11 ④ (B-11 语义修正): 原 n_drop 自称"撤单模拟", 实际是成交后删掉最差的
n 笔卖出交易重算 NAV — 这是改写历史不是模拟撤单。已改名 n_exclude 并标注
"事后剔除分析" (敏感性分析用途: 看剔除最差 N 笔后绩效如何)。
真撤单模拟 (限价单未成交即撤) 属从零写的大工程, 按 D-18 排队。
"""
from backtest_layer.core.broker import Broker
from common.logging import get_logger

logger = get_logger("backtest.signal")


class SignalMode:
    def __init__(self, broker: Broker):
        self._broker = broker

    def run(self, signals: list[dict], price_data: dict[str, list[float]],
            n_exclude: int = 0) -> dict:
        equity_curve = []
        trade_count = 0
        winning_trades = 0
        trade_log = []
        for signal in signals:
            code = signal.get("code", "")
            action = signal.get("signal", "hold")
            prices = price_data.get(code, [])
            if not prices:
                continue
            current_price = prices[-1]
            date = signal.get("date", "")
            if action == "buy":
                shares = int(self._broker.cash * 0.9 / current_price / 100) * 100
                if shares > 0:
                    result = self._broker.buy(code, current_price, shares, date)
                    if result.success:
                        trade_count += 1
                        trade_log.append({
                            "code": code, "action": "buy", "date": date,
                            "price": result.fill_price, "shares": shares,
                        })
            elif action == "sell":
                pos = self._broker.positions.get(code)
                if pos and pos["shares"] > 0:
                    result = self._broker.sell(code, current_price, pos["shares"], date)
                    if result.success:
                        trade_count += 1
                        winning_trades += 1 if result.pnl > 0 else 0
                        trade_log.append({
                            "code": code, "action": "sell", "date": date,
                            "price": result.fill_price, "shares": pos["shares"],
                            "pnl": result.pnl,
                        })
            all_prices = {c: p[-1] for c, p in price_data.items() if p}
            equity_curve.append(self._broker.nav(all_prices))

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
        }

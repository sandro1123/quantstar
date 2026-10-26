"""backtest_layer/modes/hold.py — Hold 模式（含日级 NAV 追踪 + 假日 fallback）

BT-FIX (2026-09-10, 统筹派单 sm-6d296fb2f3534a87 — 回测假数据卡):
  病理链 (probe1~17 取证): 旧版 date_range_end=signal+hold_days 自然日 → 生产跑时
  (16:30 当日) end 永在未来 → _find_valid_trading_day_reverse 10 次找不到 → 回落
  end 本身 → snapshot(未来日) 0 klines → 卖出腿裸 continue (零告警) → trades=0 但
  nav({}) 把持仓按 0 计 → total_return=-spent/cap = 假 -68%~-99% (8 条历史假行)。
  四修点:
    ① _compute_hold_end_date: hold 窗口尾部按交易日序列 (daily_klines 实际日期) 推进,
       序列不足时收敛到最后可用交易日 + partial 标记 (替代自然日 +5);
    ② 卖出腿禁静默: exit 日无该票 kline → sell_data_missing 记名 + warning, 禁裸 continue;
    ③ NAV 缺价护: 未卖出持仓按最后已知价估值 (NAV 循环的 last_prices 延伸到收盘计价),
       禁 nav({}) 把持仓按 0;
    ④ total_trades=0 守卫: 没考试没分数 — 零卖出成交时 total_return/win_rate/sharpe/
       max_drawdown 一律 0 + data_quality.valid=False (持久化/消费面据此拒收)。

BT-CORRECTNESS (2026-10, fix/backtest-correctness):
  ⑤ 成交口径: 默认 fill_price="next_open" — date_range[0] 视为信号日 (收盘后出信号),
     T+1 (下一交易日) 开盘价成交; 该票当日无 kline / open<=0 (停牌) → 跳过 (entry_skipped 记名)。
     fill_price="same_close" 复现旧口径 (信号日收盘成交, 含前视)。
  ⑥ NAV 仅按交易日 (有 kline 的日期) 记点, 与 Metrics 252 年化一致; 不再按自然日补平。
  ⑦ 涨跌停: 买卖均传 pre_close (kline.pre_close, 缺失取前一交易日收盘) → 涨停封板拒买、
     跌停封板拒卖 (sell_blocked 记名, 持仓按最后已知价估值)。
  ⑧ 交易记录 pnl = 净盈亏 (含买入佣金/过户费分摊); commission = 买入 + 卖出佣金。
"""

from datetime import datetime, timedelta

from backtest_layer.core.broker import Broker
from backtest_layer.core.metrics import Metrics
from backtest_layer.core.models import BacktestInput, BacktestReport
from backtest_layer.core.sizer import AllInSizer
from common.logging import get_logger

logger = get_logger("backtest.hold")


def _find_valid_trading_day(data_layer, start_date: str, max_attempts: int = 10) -> str:
    """从 start_date 开始向后找第一个有 kline 数据的交易日"""
    current = datetime.strptime(start_date, "%Y-%m-%d")
    for _ in range(max_attempts):
        ds = current.strftime("%Y-%m-%d")
        snap = data_layer.snapshot(ds)
        if snap and hasattr(snap, "klines") and snap.klines:
            return ds
        current += timedelta(days=1)
    return start_date


def _find_valid_trading_day_reverse(data_layer, end_date: str, max_attempts: int = 10) -> str:
    """从 end_date 开始向前找第一个有 kline 数据的交易日"""
    current = datetime.strptime(end_date, "%Y-%m-%d")
    for _ in range(max_attempts):
        ds = current.strftime("%Y-%m-%d")
        snap = data_layer.snapshot(ds)
        if snap and hasattr(snap, "klines") and snap.klines:
            return ds
        current -= timedelta(days=1)
    return end_date


def _compute_hold_end_date(signal_date: str, hold_days: int, trading_days: list[str]):
    """BT-FIX 修点①: 持有窗尾部 = signal 之后的第 hold_days 个**交易日**。

    trading_days = daily_klines 实际日期序列 (升序)。signal 当日不算持有日:
    序列中 > signal_date 的第 hold_days 个日期即窗尾。序列不足 hold_days
    (尾部超出最新数据日) 时收敛到序列最后一日并标 partial=True —
    与 orchestrator 旧 partial_window 语义对齐。

    返回 (end_date, partial)。序列为空时退化 (signal_date, True)。
    """
    if not trading_days:
        return signal_date, True
    forward = [d for d in trading_days if d > signal_date]
    if len(forward) >= hold_days:
        return forward[hold_days - 1], False
    return forward[-1] if forward else signal_date, True


FILL_NEXT_OPEN = "next_open"
FILL_SAME_CLOSE = "same_close"
_LOOKBACK_DAYS = 15


def _num(x) -> float | None:
    """只接受真实数值 (排除 MagicMock 等假属性)。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


def _kline_map(data_layer, date: str) -> dict:
    """date 当日 {code: kline}; 无快照/无 kline → 空 dict (= 非交易日或数据缺)。"""
    try:
        snap = data_layer.snapshot(date)
    except Exception:
        return {}
    klines = getattr(snap, "klines", None) if snap else None
    if not klines:
        return {}
    try:
        return {k.code: k for k in klines}
    except TypeError:
        return {}


def _find_next_trading_day(data_layer, after_date: str, max_attempts: int = 15) -> str | None:
    """严格晚于 after_date 的第一个有 kline 的交易日; 找不到 → None。"""
    current = datetime.strptime(after_date, "%Y-%m-%d")
    for _ in range(max_attempts):
        current += timedelta(days=1)
        ds = current.strftime("%Y-%m-%d")
        if _kline_map(data_layer, ds):
            return ds
    return None


def _prev_kline_map(data_layer, date: str, max_attempts: int = _LOOKBACK_DAYS) -> dict:
    """date 之前最近一个交易日的 {code: kline} (取前收盘用); 找不到 → {}。"""
    current = datetime.strptime(date, "%Y-%m-%d")
    for _ in range(max_attempts):
        current -= timedelta(days=1)
        km = _kline_map(data_layer, current.strftime("%Y-%m-%d"))
        if km:
            return km
    return {}


def _pre_close(code: str, kline, prev_map: dict) -> float | None:
    pc = _num(getattr(kline, "pre_close", None))
    if pc and pc > 0:
        return pc
    prev = prev_map.get(code)
    pc = _num(getattr(prev, "close", None)) if prev is not None else None
    return pc if pc and pc > 0 else None


def _name_of(kline) -> str | None:
    n = getattr(kline, "name", None)
    return n if isinstance(n, str) else None


def _trading_days(data_layer, start: str, end: str):
    """[start, end] 内有 kline 的日期 (升序) 及其 kline map。"""
    out = []
    try:
        d = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return out
    while d <= e:
        ds = d.strftime("%Y-%m-%d")
        km = _kline_map(data_layer, ds)
        if km:
            out.append((ds, km))
        d += timedelta(days=1)
    return out


def simulate_buy_and_hold(broker: Broker, data_layer, input: BacktestInput, sizer) -> dict:
    """hold / portfolio 共用: 信号日 → 建仓 → 交易日 NAV → 窗尾收盘卖出。"""
    start_date, end_date = input.date_range
    fill_mode = getattr(input, "fill_price", FILL_NEXT_OPEN) or FILL_NEXT_OPEN
    if fill_mode not in (FILL_NEXT_OPEN, FILL_SAME_CLOSE):
        raise ValueError(f"unknown fill_price: {fill_mode!r}")
    res = {"fill_mode": fill_mode, "entry_date": None, "exit_date": None,
           "buy_results": {}, "equity_curve": [], "trade_log": [],
           "sell_data_missing": [], "sell_blocked": [], "entry_skipped": [],
           "nav": broker.cost.initial_capital, "last_prices": {}}

    if fill_mode == FILL_NEXT_OPEN:
        entry_date = _find_next_trading_day(data_layer, start_date)
        if entry_date is None or entry_date > end_date:
            res["entry_skipped"] = list(input.codes)
            return res
        curve_anchor = start_date
    else:
        entry_date = _find_valid_trading_day(data_layer, start_date)
        curve_anchor = entry_date
    exit_date = _find_valid_trading_day_reverse(data_layer, end_date)
    res["entry_date"], res["exit_date"] = entry_date, exit_date

    entry_map = _kline_map(data_layer, entry_date)
    prev_map = _prev_kline_map(data_layer, entry_date) if entry_map else {}
    buy_results = res["buy_results"]
    for code in input.codes:
        kline = entry_map.get(code)
        if kline is None:
            res["entry_skipped"].append(code)
            continue
        field_name = "open" if fill_mode == FILL_NEXT_OPEN else "close"
        price = _num(getattr(kline, field_name, None))
        if not price or price <= 0:
            res["entry_skipped"].append(code)  # 停牌/缺价 → 跳过, 不编造
            continue
        # BT-11 ①: Sizer 接线 — 仓位计算走 AllInSizer (核心风控, G-B22)
        shares = sizer.size(broker.cost.initial_capital, price, num_positions=len(input.codes))
        if shares <= 0:
            continue
        # BT-10: 量能限制接真实 volume; BT-CORRECTNESS ⑦: pre_close 传入 → 涨停封板拒买
        result = broker.buy(code, price, shares, entry_date,
                            volume=getattr(kline, "volume", None),
                            pre_close=_pre_close(code, kline, prev_map),
                            name=_name_of(kline))
        if result.success:
            buy_results[code] = {
                "shares": result.shares or shares,
                "entry_price": result.fill_price,
                "entry_date": entry_date,
                "commission": result.commission,
                "transfer_fee": result.transfer_fee,
            }
        else:
            res["entry_skipped"].append(code)
    if not buy_results:
        return res

    # NAV: 仅交易日记点 (BT-CORRECTNESS ⑥)
    equity_curve = res["equity_curve"]
    held_codes = list(buy_results.keys())
    last_prices = {code: buy_results[code]["entry_price"] for code in held_codes}
    res["last_prices"] = last_prices
    equity_curve.append({"date": curve_anchor, "nav": broker.cost.initial_capital})
    for date_str, km in _trading_days(data_layer, entry_date, exit_date):
        if date_str == curve_anchor:
            continue
        for code in held_codes:
            close = _num(getattr(km.get(code), "close", None)) if code in km else None
            if close:
                last_prices[code] = close
        equity_curve.append({"date": date_str, "nav": broker.nav(dict(last_prices))})

    # 卖出 — BT-FIX ②: 缺价记名 + warning, 禁裸 continue
    exit_map = _kline_map(data_layer, exit_date)
    exit_prev_map = _prev_kline_map(data_layer, exit_date) if exit_map else {}
    for code, buy_info in buy_results.items():
        kline = exit_map.get(code)
        price = _num(getattr(kline, "close", None)) if kline is not None else None
        if not price:
            res["sell_data_missing"].append(code)
            logger.warning(
                f"Backtest sell skipped: no kline/close for {code} at exit_date={exit_date} "
                f"(entry {buy_info['entry_date']}) — data_missing")
            continue
        result = broker.sell(code, price, buy_info["shares"], exit_date,
                             pre_close=_pre_close(code, kline, exit_prev_map),
                             name=_name_of(kline))
        if not result.success:
            res["sell_blocked"].append(code)
            logger.warning(f"Backtest sell refused for {code} at {exit_date}: {result.reason}")
            continue
        res["trade_log"].append({
            "code": code,
            "entry_date": buy_info["entry_date"],
            "exit_date": exit_date,
            "entry_price": buy_info["entry_price"],
            "exit_price": result.fill_price,
            "shares": buy_info["shares"],
            # BT-CORRECTNESS ⑧: 净盈亏 (含买入佣金) / 佣金 = 买 + 卖
            "pnl": result.pnl,
            "gross_pnl": result.gross_pnl,
            "commission": result.commission + result.buy_commission,
            "stamp_tax": result.stamp_tax,
            "transfer_fee": result.transfer_fee + result.buy_transfer_fee,
        })

    # BT-FIX ③: 收盘 NAV — 未卖出持仓按最后已知价估值 (禁 nav({}) 把持仓按 0)
    nav = broker.nav({code: last_prices.get(code, 0.0) for code in broker.positions})
    res["nav"] = nav
    if equity_curve and equity_curve[-1]["date"] == exit_date:
        equity_curve[-1] = {"date": exit_date, "nav": nav}
    else:
        equity_curve.append({"date": exit_date, "nav": nav})
    return res


def cost_summary(trade_log: list[dict], nav: float, capital: float) -> tuple[float, float]:
    """(pre_cost_return, cost_impact) — 费用 = 佣金(买+卖) + 印花税 + 过户费。"""
    total_cost = sum(t.get("commission", 0) + t.get("stamp_tax", 0) + t.get("transfer_fee", 0)
                     for t in trade_log)
    if capital <= 0:
        return 0.0, 0.0
    return (nav + total_cost) / capital - 1, total_cost / capital


class HoldMode:
    def __init__(self, broker: Broker, data_layer, strategy_layer):
        self.broker = broker
        self.data_layer = data_layer
        self.strategy_layer = strategy_layer
        # BT-11 ①: 仓位计算接 AllInSizer (资金等分, 100 股取整) — 替代内联算式
        self._sizer = AllInSizer()

    def run(self, input: BacktestInput) -> BacktestReport:
        broker = self.broker
        sim = simulate_buy_and_hold(broker, self.data_layer, input, self._sizer)
        equity_curve = sim["equity_curve"]
        trade_log = sim["trade_log"]
        sell_data_missing = sim["sell_data_missing"]
        exit_date = sim["exit_date"]
        nav = sim["nav"]
        extra_dq = {"fill_price": sim["fill_mode"], "entry_date": sim["entry_date"],
                    "entry_skipped": sim["entry_skipped"], "sell_blocked": sim["sell_blocked"]}

        if not sim["buy_results"]:
            return BacktestReport(
                mode=input.mode, date_range=input.date_range,
                total_return=0.0, annual_return=0.0, sharpe=0.0,
                max_drawdown=0.0, win_rate=0.0, total_trades=0,
                sell_data_missing=[], data_quality={"valid": False, "reason": "no_entry", **extra_dq},
            )

        # BT-FIX ④: total_trades=0 守卫 — 没考试没分数。
        if not trade_log:
            return BacktestReport(
                mode=input.mode,
                date_range=input.date_range,
                total_return=0.0, annual_return=0.0, sharpe=0.0,
                max_drawdown=0.0, win_rate=0.0, total_trades=0,
                equity_curve=equity_curve,
                trade_log=trade_log,
                sell_data_missing=sell_data_missing,
                data_quality={
                    "valid": False,
                    "reason": "zero_sell_trades",
                    "sell_data_missing_n": len(sell_data_missing),
                    "exit_date": exit_date,
                    **extra_dq,
                },
            )

        nav_series = [e["nav"] for e in equity_curve]
        # BT-06: SQN 用每笔交易 pnl 真算 (无 trade_log 时 Metrics.calc 内标 None)
        trade_pnls = [t["pnl"] for t in trade_log if "pnl" in t]
        metrics = Metrics.calc(nav_series, trade_pnls=trade_pnls or None)
        total_return = (nav / broker.cost.initial_capital - 1) if broker.cost.initial_capital > 0 else 0.0
        winning_trades = [t for t in trade_log if t["pnl"] > 0]
        win_rate = len(winning_trades) / len(trade_log) if trade_log else 0.0

        # 成本前后对比
        pre_cost_return, cost_impact = cost_summary(trade_log, nav, broker.cost.initial_capital)

        pf = Metrics.profit_factor(trade_log)
        exp = Metrics.expectancy(trade_log)

        if total_return > 0:
            one_line_verdict = f"收益 {total_return:.1%}，夏普 {metrics.sharpe:.2f}，策略表现"
        else:
            one_line_verdict = f"收益 {total_return:.1%}，最大回撤 {metrics.max_drawdown:.1%}，需优化"

        return BacktestReport(
            mode=input.mode,
            date_range=input.date_range,
            total_return=round(total_return, 4),
            annual_return=metrics.annual_return,
            sharpe=metrics.sharpe,
            sortino=metrics.sortino,
            calmar=metrics.calmar,
            omega=metrics.omega,
            deflated_sharpe=metrics.deflated_sharpe,
            profit_factor=round(pf, 4) if pf != float("inf") else 9999.0,
            expectancy=round(exp, 4),
            max_drawdown=metrics.max_drawdown,
            win_rate=round(win_rate, 4),
            total_trades=len(trade_log),
            pre_cost_return=round(pre_cost_return, 4),
            post_cost_return=round(total_return, 4),
            cost_impact_pct=round(cost_impact * 100, 4),
            equity_curve=equity_curve,
            trade_log=trade_log,
            one_line_verdict=one_line_verdict,
            sell_data_missing=sell_data_missing,
            data_quality={
                "valid": True,
                "sell_data_missing_n": len(sell_data_missing),
                "exit_date": exit_date,
                **extra_dq,
            },
        )

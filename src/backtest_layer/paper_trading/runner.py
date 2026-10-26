"""backtest_layer/paper_trading/runner.py — 纸交易运行器 (BT-12 / D-15 立项, D-24 对齐)

纸交易身兼四职 (用户拍板立项):
  ① 持续监控: 每日消费 stock_pool 真实推荐信号模拟持有, 跟踪纸面盈亏
     (回测只验证过去, 纸交易验证"当下还灵不灵")。
  ② 滑点实测: reconciliation 对比信号价 vs 成交价 (均读自执行层账本, 只读导出)。
  ③ WFO 门禁中间档: Efficiency 0.5-0.8 的权重去向 — BT-05 裁定载体为
     orchestrator 写的 paper_trading_weights 表; 本层只读消费, 不建表。
  ④ 实盘毕业闸门: 纸交易 Sharpe > 回测 Sharpe × 0.7 才建议上小资金实盘
     (只建议, 从不自动上实盘)。

D-24 裁定: 两个账本记同一件事 = 债务。PaperBroker 保留唯一生产纸账本;
本运行器退掉自建账 (virtual_capital 内存资金线/自记账全部删除):
  - 执行: 委托执行层 PaperBroker.entry_with_fees (写 stock_pool/action_records)。
  - 资金: 统一 execution.initial_capital; 现金/持仓/盈亏全读执行层账本。
  - 信号源: stock_pool (真实推荐, added_date=信号日)。
  - 评估输出: paper_trading_stats (config 门控, 默认关)。

config 门控 (默认关, 对齐 BT-03/ST-13 哲学):
  backtest.paper_trading.enabled = true 才建/写 paper_trading_stats 评估表;
  账本消费 (读 stock_pool / 经 PaperBroker 成交) 不受此门控 — 那是职责本身。
"""
from dataclasses import dataclass, field

from backtest_layer.paper_trading.bridge import PaperTradingBridge
from common.logging import get_logger

logger = get_logger("backtest.paper_trading")

# 毕业闸门: 纸交易 Sharpe > 回测 Sharpe × 0.7 (设计 BT-12 ④)
GRADUATION_SHARPE_RATIO = 0.7
# 毕业前置: 纸交易至少跑满 30 天
GRADUATION_MIN_DAYS = 30

# D-24: paper_trading 成交表已退役 — 成交账由执行层 stock_pool/action_records
# 唯一承载 (信号价 ← stock_pool.entry_low/entry_high, 成交价 ← action_records
# entry/exit 的 price)。本文件不再保留其 DDL。

CREATE_PAPER_TRADING_STATS_SQL = """
CREATE TABLE IF NOT EXISTS paper_trading_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    total_signals INTEGER,
    filled_signals INTEGER,
    avg_slippage REAL,
    max_slippage REAL,
    paper_return REAL,
    benchmark_return REAL,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""


@dataclass
class PaperSignal:
    """从 stock_pool 读出的真实推荐信号"""
    code: str
    signal_date: str
    signal_price: float
    source: str = "strategy"
    name: str = ""


@dataclass
class DailyPaperResult:
    date: str = ""
    n_signals: int = 0
    n_filled: int = 0
    slippages: list = field(default_factory=list)
    paper_return: float = 0.0
    persisted: bool = False


def read_signals_from_stock_pool(db, date: str) -> list[PaperSignal]:
    """BT-12: 从 stock_pool 读当日真实推荐信号 (added_date = 信号日)。

    信号价 = 入场区间中值 (entry_low/entry_high); 区间缺失回退 entry_price;
    都无 → 0 由调用方回退行情收盘价。无 db / 无表 / 无当日信号 → 空 list, 不造假。
    """
    if db is None:
        return []
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT code, name, entry_low, entry_high, entry_price, source "
            "FROM stock_pool WHERE added_date = ?",
            (date,),
        ).fetchall()
    except Exception:
        return []
    signals = []
    for r in rows:
        def col(name, idx):
            return r[name] if hasattr(r, "keys") else r[idx]
        code = col("code", 0)
        if not code:
            continue
        entry_low, entry_high = col("entry_low", 2), col("entry_high", 3)
        entry_price = col("entry_price", 4)
        if entry_low and entry_high:
            signal_price = (float(entry_low) + float(entry_high)) / 2
        elif entry_price:
            signal_price = float(entry_price)
        else:
            signal_price = 0.0
        signals.append(PaperSignal(
            code=code, signal_date=date, signal_price=signal_price,
            source=col("source", 5) or "strategy", name=col("name", 1) or "",
        ))
    return signals


def get_close_price(data_layer, code: str, date: str) -> float | None:
    """从 data_layer snapshot 读当日收盘价; 缺失返回 None (不造假)。"""
    if data_layer is None:
        return None
    try:
        snap = data_layer.snapshot(date)
        if not snap or not hasattr(snap, "klines"):
            return None
        kline = next((k for k in snap.klines if getattr(k, "code", None) == code), None)
        close = getattr(kline, "close", None) if kline else None
        return float(close) if close else None
    except Exception:
        return None


# BT-CORRECTNESS ①: 成交口径。"next_open" (默认) = 信号日 (T) 的推荐在 T+1 开盘成交 —
# run_daily(date) 消费上一交易日的信号, 按 date 开盘价成交; "same_close" = 旧口径
# (信号日收盘成交, 含前视), 仅供复现。config 键: backtest.paper_trading.fill_price。
DEFAULT_FILL_PRICE = "next_open"


def get_open_price(data_layer, code: str, date: str) -> float | None:
    """从 data_layer snapshot 读当日开盘价; 缺失/停牌 (open<=0) 返回 None (不造假)。"""
    if data_layer is None:
        return None
    try:
        snap = data_layer.snapshot(date)
        if not snap or not hasattr(snap, "klines"):
            return None
        kline = next((k for k in snap.klines if getattr(k, "code", None) == code), None)
        op = getattr(kline, "open", None) if kline else None
        return float(op) if isinstance(op, (int, float)) and op > 0 else None
    except Exception:
        return None


def read_wfo_paper_weights(db, date: str | None = None) -> list[dict]:
    """BT-12 ③: 只读消费 WFO 中间档权重。

    BT-05 裁定的现载体 = orchestrator._apply_wfo_tier 写的 paper_trading_weights
    (backtest.wfo_paper_persist.enabled 门控, 默认关)。本函数只读不建表;
    无 db / 表不存在 / 无记录 → 空 list。
    """
    if db is None:
        return []
    try:
        conn = db.get_connection()
        if date:
            rows = conn.execute(
                "SELECT assign_date, factor_name, weight, source "
                "FROM paper_trading_weights WHERE assign_date = ? "
                "ORDER BY factor_name", (date,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT assign_date, factor_name, weight, source "
                "FROM paper_trading_weights ORDER BY assign_date DESC, factor_name",
            ).fetchall()
    except Exception:
        return []
    return [
        {"assign_date": r[0] if not hasattr(r, "keys") else r["assign_date"],
         "factor_name": r[1] if not hasattr(r, "keys") else r["factor_name"],
         "weight": r[2] if not hasattr(r, "keys") else r["weight"],
         "source": r[3] if not hasattr(r, "keys") else r["source"]}
        for r in rows
    ]


class PaperTradingRunner:
    """纸交易运行器 — 信号源接线 + 委托执行层成交 + reconciliation + 毕业检查。

    D-24: 无自建账。成交经 PaperBroker 落执行层 stock_pool/action_records;
    现金/持仓/盈亏/净值全读账本 (PaperTradingBridge 只读视图)。
    """

    def __init__(self, db=None, data_layer=None, config=None,
                 virtual_capital: float | None = None, fill_price: str | None = None):
        self.db = db
        self._fill_price = fill_price
        self.data_layer = data_layer
        self.config = config
        # virtual_capital 仅为兼容旧调用方 (orchestrator) 保留, D-24 后忽略 —
        # 资金基准统一 execution.initial_capital。
        self.bridge = PaperTradingBridge(db=db, config=config)
        self._enabled: bool | None = None  # 惰性判定, 未启用不建评估表

    def wfo_weights_on_test(self) -> dict | None:
        """#108 读腿: 摘要 paper_trading_weights (WFO 中间档权重在纸交易
        验证中的可观测面) — read_wfo_paper_weights 的真实消费者。

        消费链: wfo_gate paper_trading 档 (orchestrator._apply_wfo_tier,
        backtest.wfo_paper_persist.enabled 门控) 写 paper_trading_weights
        → 本方法摘要 → _run_paper_trading meta 挂载 (铁律 #12 可观测)。
        纯只读, 不影响打分/执行; 空表/无 db → None (诚实空态)。
        """
        try:
            rows = read_wfo_paper_weights(self.db)
        except Exception:
            return None
        if not rows:
            return None
        latest = max(r["assign_date"] for r in rows)
        latest_rows = [r for r in rows if r["assign_date"] == latest]
        return {
            "n_weights": len(rows),
            "latest_assign_date": latest,
            "n_latest": len(latest_rows),
            "top_factors": sorted(
                ({"factor": r["factor_name"], "weight": round(float(r["weight"]), 6)}
                 for r in latest_rows),
                key=lambda x: -abs(x["weight"]))[:5],
        }

    def _persistence_enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        cfg = self.config
        self._enabled = bool(cfg.get("backtest.paper_trading.enabled", False)) if cfg else False
        return self._enabled

    def _ensure_stats_table(self) -> bool:
        """建评估输出表 (仅启用时)。返回是否就绪。"""
        if not self._persistence_enabled() or self.db is None:
            return False
        try:
            conn = self.db.get_connection()
            conn.execute(CREATE_PAPER_TRADING_STATS_SQL)
            conn.commit()
            return True
        except Exception as e:
            logger.warning(f"paper_trading_stats 建表失败: {e}")
            return False

    def _broker(self):
        """执行层 PaperBroker (唯一纸账本执行入口); 无 db → None。

        D-280: dry_run 接线 — 有 config 时读 execution.dry_run (默认 true, 对齐
        intraday/monitor.py 口径); 无 config 裸构造保持既有 False 语义不扰单测。
        """
        if self.db is None:
            return None
        from execution_layer.core.paper_broker import PaperBroker
        from execution_layer.core.stock_pool import StockPool
        dry_run = bool(self.config.get("execution.dry_run", True)) if self.config else False
        return PaperBroker(self.db, StockPool(self.db, self.config), config=self.config,
                           dry_run=dry_run)

    def fill_mode(self) -> str:
        mode = self._fill_price
        if not mode and self.config is not None:
            try:
                mode = self.config.get("backtest.paper_trading.fill_price", None)
            except Exception:
                mode = None
        mode = mode or DEFAULT_FILL_PRICE
        if mode not in ("next_open", "same_close"):
            raise ValueError(f"unknown paper fill_price: {mode!r}")
        return mode

    def signal_date_for(self, date: str) -> str:
        """成交日 date 对应的信号日: next_open → 上一交易日; same_close → 当日。"""
        if self.fill_mode() == "same_close":
            return date
        from common.calendar import TradingCalendar
        return TradingCalendar(self.db).prev_trading_day(date)

    def run_daily(self, date: str) -> DailyPaperResult:
        """每日纸交易: 读 stock_pool 当日信号 → 委托 PaperBroker 买入 → 账本留痕。

        成交价 = next_open (默认): 消费上一交易日信号, 按 date 开盘价成交;
        same_close: 当日信号按当日收盘价成交 (旧口径)。无行情回退信号价 (滑点记 0,
        不编造行情); 两者都无 → 跳过该信号。信号价缺失时同样回退收盘价。
        """
        result = DailyPaperResult(date=date)
        next_open = self.fill_mode() == "next_open"
        signal_date = self.signal_date_for(date)
        signals = read_signals_from_stock_pool(self.db, signal_date)
        result.n_signals = len(signals)
        if not signals:
            logger.info(f"Paper trading {date}: 无当日信号 (stock_pool.added_date)")
            return result
        broker = self._broker()
        if broker is None:
            return result

        for sig in signals:
            item = broker.stock_pool.get(sig.code)
            if not item or item["status"] not in ("watch", "pending_entry"):
                continue  # 已持仓/已退出/待退出 → 不重复建仓

            signal_price = sig.signal_price
            # next_open: 信号日 T 的推荐按 date(=T+1) 开盘价成交; same_close: 当日收盘 (旧口径)
            fill_price = (get_open_price(self.data_layer, sig.code, date) if next_open
                          else get_close_price(self.data_layer, sig.code, date))
            if fill_price is None:
                fill_price = signal_price  # 无行情 → 按信号价成交, 滑点 0 (不编造)
            if not fill_price or fill_price <= 0:
                continue  # 无真实价格 → 跳过, 不编造
            if signal_price <= 0:
                sig.signal_price = fill_price  # 记录实际采用的信号价, 不记 0
                signal_price = fill_price

            if item["status"] == "watch":
                broker.stock_pool.update_status(sig.code, "pending_entry")
                item = broker.stock_pool.get(sig.code)

            res = broker.entry_with_fees(
                sig.code, fill_price,
                f"BT-12 纸交易建仓 (信号日 {signal_date}, 成交日 {date}, 来源 {sig.source})", item)
            if not res.success:
                logger.info(f"Paper entry {sig.code} 未成交: {res.reason}")
                continue
            result.n_filled += 1
            result.slippages.append((res.price - signal_price) / signal_price)

        initial = self.bridge.initial_capital
        result.paper_return = (self.bridge.portfolio_value() / initial - 1) if initial else 0.0
        result.persisted = self._persistence_enabled() and self.db is not None
        logger.info(
            f"Paper trading {date}: {result.n_filled}/{result.n_signals} filled, "
            f"return={result.paper_return:.2%}")
        return result

    def reconcile(self, date: str) -> dict:
        """BT-12 ②: 滑点实测 — 信号价 vs 成交价, 两者均只读自执行层账本。

        信号价 = stock_pool 入场区间中值 (缺失 → 该信号不计滑点, 不造假);
        成交价 = action_records 当日 entry 的 price。写 paper_trading_stats (门控)。
        """
        stats = {"date": date, "total_signals": 0, "filled_signals": 0,
                 "avg_slippage": None, "max_slippage": None,
                 "paper_return": None, "benchmark_return": None}
        if self.db is None:
            return stats
        signals = read_signals_from_stock_pool(self.db, self.signal_date_for(date))
        stats["total_signals"] = len(signals)
        if not signals:
            return stats

        fills = {f["code"]: f for f in self.bridge.fills(date) if f.get("action_type") == "entry"}
        slippages = []
        for sig in signals:
            fill = fills.get(sig.code)
            if fill is None or fill.get("price") is None:
                continue
            stats["filled_signals"] += 1
            if sig.signal_price > 0:
                slippages.append((float(fill["price"]) - sig.signal_price) / sig.signal_price)

        if slippages:
            stats["avg_slippage"] = round(sum(slippages) / len(slippages), 6)
            stats["max_slippage"] = round(max(abs(s) for s in slippages), 6)
        initial = self.bridge.initial_capital
        stats["paper_return"] = round(
            self.bridge.portfolio_value() / initial - 1, 6) if initial else None
        # D8 通电: 基准收益 — 沪深300 当日收盘/前一交易日收盘 (DL-23 读 API)
        stats["benchmark_return"] = self._benchmark_return(date)

        self._record_stats(stats)
        return stats

    def _benchmark_return(self, date: str) -> float | None:
        """D8: 基准当日收益 — 沪深300 (000300.SH) 当日收盘 / 前一交易日收盘 - 1。

        数据源 = DL-23 get_index_daily 读 API (index_daily 表, 管线已通电)。
        无 data_layer / 指数缺数 / 当日无收盘 / 前一交易日缺失 → None (不造假)。
        口径注: 当日收益; paper_return 为累计口径, 两者语义不同 (报统筹备案)。
        """
        if self.data_layer is None:
            return None
        code = "000300.SH"
        if self.config:
            code = self.config.get("backtest.paper_trading.benchmark_code", code) or code
        try:
            rows = self.data_layer.get_index_daily(code, end_date=date)
        except Exception:
            return None
        if not rows or len(rows) < 2:
            return None
        last, prev = rows[-1], rows[-2]
        if last.get("date") != date or not last.get("close") or not prev.get("close"):
            return None
        return round(last["close"] / prev["close"] - 1, 6)

    def _record_stats(self, stats: dict) -> None:
        if not self._ensure_stats_table():
            return
        try:
            conn = self.db.get_connection()
            conn.execute(
                "INSERT INTO paper_trading_stats "
                "(date, total_signals, filled_signals, avg_slippage, max_slippage, "
                " paper_return, benchmark_return) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (stats["date"], stats["total_signals"], stats["filled_signals"],
                 stats["avg_slippage"], stats["max_slippage"],
                 stats["paper_return"], stats["benchmark_return"]),
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"paper_trading_stats 写入失败: {e}")

    def compute_paper_sharpe(self, days: int = 60) -> float | None:
        """BT-12 ④ 数据源: 纸交易 Sharpe — 读执行层账本净值曲线 (PnLTracker)。

        净值点 < 2 或波动为 0 → None (不可算, 不造假)。年化 √252。
        """
        if self.db is None:
            return None
        try:
            from execution_layer.core.stock_pool import StockPool
            from execution_layer.tracking.pnl_tracker import PnLTracker
            tracker = PnLTracker(self.db, StockPool(self.db, self.config), config=self.config)
            curve = tracker.get_portfolio_value_curve(days=days)
        except Exception:
            return None
        navs = [p["nav"] for p in curve if p.get("nav")]
        if len(navs) < 2:
            return None
        rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs)) if navs[i - 1]]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = var ** 0.5
        if std == 0:
            return None
        return mean / std * (252 ** 0.5)

    def check_graduation(self, backtest_sharpe: float, paper_start_date: str,
                         current_date: str) -> dict:
        """BT-12 ④: 毕业闸门 — 纸交易 Sharpe > 回测 Sharpe × 0.7 才建议上实盘。

        只产出毕业/继续建议, 从不自动上实盘。跑未满 GRADUATION_MIN_DAYS 天 → 继续。
        paper_sharpe 读自执行层账本净值曲线 (不可算 → 继续)。
        """
        from datetime import datetime
        try:
            start = datetime.strptime(paper_start_date, "%Y-%m-%d")
            now = datetime.strptime(current_date, "%Y-%m-%d")
            days = (now - start).days
        except (ValueError, TypeError):
            return {"decision": "continue", "reason": "invalid_dates"}

        if days < GRADUATION_MIN_DAYS:
            return {"decision": "continue",
                    "reason": f"纸交易仅运行 {days} 天 < {GRADUATION_MIN_DAYS} 天",
                    "days": days}
        return self.evaluate_graduation(self.compute_paper_sharpe(), backtest_sharpe, days)

    def evaluate_graduation(self, paper_sharpe: float | None, backtest_sharpe: float,
                            days: int) -> dict:
        """毕业判定核心: 给定纸交易 Sharpe 与运行天数, 产出毕业/继续建议。"""
        if days < GRADUATION_MIN_DAYS:
            return {"decision": "continue",
                    "reason": f"运行 {days} 天 < {GRADUATION_MIN_DAYS} 天", "days": days}
        if paper_sharpe is None:
            return {"decision": "continue", "reason": "纸交易 Sharpe 不可算", "days": days}
        threshold = backtest_sharpe * GRADUATION_SHARPE_RATIO
        if paper_sharpe > threshold:
            return {"decision": "graduate",
                    "reason": f"纸交易 Sharpe {paper_sharpe:.2f} > 回测×0.7 ({threshold:.2f})",
                    "days": days, "paper_sharpe": paper_sharpe,
                    "threshold_sharpe": round(threshold, 4)}
        return {"decision": "continue",
                "reason": f"纸交易 Sharpe {paper_sharpe:.2f} <= 回测×0.7 ({threshold:.2f})",
                "days": days, "paper_sharpe": paper_sharpe,
                "threshold_sharpe": round(threshold, 4)}

    def run_graduation_check(self, current_date: str) -> dict:
        """D7 通电: 毕业闸门检查入口 — paper_trading 管道尾部调用。

        参数走 config (backtest.paper_trading.graduation.backtest_sharpe /
        start_date); 缺失诚实返回 skip, 不造假判定。只产出毕业/继续建议,
        从不自动上实盘。
        """
        cfg = self.config
        if not cfg:
            return {"decision": "skip", "reason": "no_config"}
        backtest_sharpe = cfg.get("backtest.paper_trading.graduation.backtest_sharpe")
        start_date = cfg.get("backtest.paper_trading.graduation.start_date")
        if backtest_sharpe is None or not start_date:
            return {"decision": "skip",
                    "reason": "graduation config missing (backtest_sharpe/start_date)"}
        try:
            return self.check_graduation(float(backtest_sharpe), str(start_date),
                                         current_date)
        except Exception as e:  # noqa: BLE001 — 闸门失败不拖垮管道
            return {"decision": "skip", "reason": f"check failed: {e}"}

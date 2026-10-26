"""Execution Layer — 股票池全生命周期管理"""

from __future__ import annotations

from typing import Any

from common.database import Database
from common.decorators import catch_quantstar_error, with_timeout
from common.logging import get_logger
from common.utils import bj_date, generate_trace_id
from execution_layer.analysis.analyzers import SQNAnalyzer
from execution_layer.core.action_record import ActionRecord
from execution_layer.core.entry_engine import EntryEngine
from execution_layer.core.exit_engine import ExitEngine
from execution_layer.core.plan_manager import PlanManager
from execution_layer.core.rebalance_engine import RebalanceEngine
from execution_layer.core.stock_pool import StockPool
from execution_layer.stats.display import (
    AnalyzerResult,
    BestWorstTradeCard,
    CashFlowRow,
    DailyPriceRange,
    DrawdownAnalysis,
    ExitReview,
    ExitReviewCard,
    MonthlyHeatmap,
    MonthlyStats,
    MultiRiskMetrics,
    OrderState,
    PortfolioCurvePoint,
    PositionCardV2,
    ReturnAttribution,
    RiskContribution,
    RiskExpectationCard,
    RollingMetrics,
    StrategySummaryCard,
    TradeGrouping,
    TradeRecord,
    ValueCurvePoint,
)
from execution_layer.stats.performance import ExecutionStats
from execution_layer.tables import init_execution_tables
from execution_layer.tracking.pnl_tracker import PnLTracker
from execution_layer.tracking.post_exit_tracker import PostExitTracker
import sqlite3

logger = get_logger("execution")


class ExecutionLayer:
    def __init__(self, db: Database | None = None, config: Any | None = None,
                 data_layer: object | None = None, strategy_layer: object | None = None,
                 agent_layer: object | None = None, trace_id: str | None = None):
        self._trace_id = trace_id or generate_trace_id()
        self.db = db or Database()
        self.config = config
        self.data_layer = data_layer
        self.strategy_layer = strategy_layer
        self.agent_layer = agent_layer
        self.stock_pool = StockPool(self.db, config)
        self.action_record = ActionRecord(self.db)
        self.entry_engine = EntryEngine(self.db, self.stock_pool, agent_layer=self.agent_layer, data_layer=self.data_layer, config=config)
        self.exit_engine = ExitEngine(self.db, self.stock_pool, agent_layer=self.agent_layer, config=config)
        self.plan_manager = PlanManager(self.db, self.stock_pool, self.action_record)
        # EX-1: 传 config — PnLTracker 读 execution.initial_capital (原硬编码 300000)
        self.pnl_tracker = PnLTracker(self.db, self.stock_pool, config=getattr(self, 'config', None))
        self.post_exit_tracker = PostExitTracker(self.db, self.stock_pool)
        self.execution_stats = ExecutionStats(self.db)
        self.rebalance_engine = RebalanceEngine(self.db, self.stock_pool, config=config)
        self.analyzers = {
            "sqn": SQNAnalyzer(self.db),
        }
        from execution_layer.intraday.monitor import IntradayMonitor
        from execution_layer.intraday.touch_card import TouchCardPusher
        # R2: 触价行动卡推送器 — 构造零副作用 (delivery 懒加载), 仅 _scan 触发时推卡
        touch_card = TouchCardPusher(self.db, self.stock_pool, config=config)
        self.intraday_monitor = IntradayMonitor(self.db, self.stock_pool, self.entry_engine, self.exit_engine, data_layer=self.data_layer, agent_layer=self.agent_layer, config=config, touch_card=touch_card)
        self._started = False

    def start(self):
        if not self._started:
            conn = self.db.get_connection()
            init_execution_tables(conn)
            from execution_layer.migrations import ensure_schema
            ensure_schema(self.db)
            self._started = True

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=10)
    def get_portfolio_snapshot(self) -> PortfolioSnapshot:  # noqa: F821
        from common.utils import bj_date
        from execution_layer.stats.display import PortfolioSnapshot, PositionCardV2
        counts = self.stock_pool.count_by_status()

        total_invested = 0.0
        total_current = 0.0
        positions = []
        for pos in self.stock_pool.get_by_status("active"):
            entry_price = pos["entry_price"] or 0
            position_qty = pos["position_qty"] or 0
            last_price = pos["last_price"] or entry_price
            total_invested += entry_price * position_qty
            total_current += last_price * position_qty
            pnl_pct = ((last_price - entry_price) / entry_price * 100) if entry_price > 0 else 0.0
            positions.append(PositionCardV2(
                code=pos["code"],
                name=pos.get("name", ""),
                entry_price=entry_price,
                current_price=last_price,
                pnl_pct=round(pnl_pct, 2),
                holding_days=pos.get("holding_days", 0) or 0,
                stop_loss=pos.get("stop_loss", 0) or 0,
                take_profit=pos.get("take_profit", 0) or 0,
                score=pos.get("current_score", 0) or 0,
                entry_reason=pos.get("entry_reason", "") or "",
                position_pct=round((entry_price * position_qty / max(total_invested, 1)) * 100, 2),
            ))

        pending_entries = [
            {"code": p["code"], "name": p.get("name", ""),
             "entry_low": p.get("entry_low", 0), "entry_high": p.get("entry_high", 0),
             "score": p.get("current_score", 0) or 0}
            for p in self.stock_pool.get_by_status("pending_entry")
        ]
        pending_exits = [
            {"code": p["code"], "name": p.get("name", ""),
             "exit_reason": p.get("exit_reason", "") or "",
             "unrealized_pnl_pct": p.get("unrealized_pnl_pct", 0) or 0}
            for p in self.stock_pool.get_by_status("exit_pending")
        ]

        total_pnl = total_current - total_invested
        total_pnl_pct = total_pnl / total_invested if total_invested > 0 else 0.0

        total_capital = getattr(self.rebalance_engine, 'total_capital', 300000.0)
        available_cash = self.stock_pool.get_available_cash(total_capital)
        portfolio_nav = available_cash + total_current

        return PortfolioSnapshot(
            positions=positions,
            pending_entries=pending_entries,
            pending_exits=pending_exits,
            active=counts.get("active", 0),
            pending_entry=counts.get("pending_entry", 0),
            pending_exit=counts.get("exit_pending", 0),
            watch=counts.get("watch", 0),
            exited=counts.get("exited", 0),
            total_invested=round(total_invested, 2),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl_pct, 4),
            total_capital=total_capital,
            portfolio_nav=round(portfolio_nav, 2),
            today_pnl=round(total_pnl, 2),
            available_cash=round(available_cash, 2),
            date=bj_date(),
        )

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=10)
    def filter_trades(self, status: str = "", sector: str = "",
                      min_return: float = -999, max_return: float = 999,
                      date_from: str = "", date_to: str = "",
                      limit: int = 1000) -> list[TradeRecord]:
        """Filter trades by status, return range, date range"""
        rows = self.stock_pool.filter_by_criteria(
            status=status, min_return=min_return, max_return=max_return,
            date_from=date_from, date_to=date_to, limit=limit
        )
        return [
            TradeRecord(
                code=r.get("code", ""),
                name=r.get("name", ""),
                status=r.get("status", ""),
                entry_price=r.get("entry_price", 0) or 0,
                exit_price=r.get("exit_price", 0) or 0,
                total_return_pct=r.get("total_return_pct", 0) or 0,
                holding_days=r.get("holding_days_actual", 0) or 0,
                added_date=r.get("added_date", ""),
                exit_date=r.get("exit_date", ""),
                source=r.get("source", ""),
                entry_reason=r.get("added_reason", ""),
                exit_reason=r.get("exit_reason", ""),
            )
            for r in rows
        ]

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_exit_review(self, code: str) -> ExitReviewCard | None:
        from execution_layer.stats.display import ExitReviewCard
        pool = self.stock_pool.get(code)
        if not pool or pool.get("status") != "exited":
            return None
        return ExitReviewCard(
            code=code,
            name=pool.get("name", ""),
            entry_price=pool.get("entry_price", 0) or 0,
            exit_price=pool.get("exit_price", 0) or 0,
            total_return_pct=pool.get("total_return_pct", 0) or 0,
            holding_days=pool.get("holding_days_actual", 0) or 0,
            exit_reason=pool.get("exit_reason", ""),
            exit_reason_user=pool.get("exit_reason_detail", ""),
            post_exit_t=self.post_exit_tracker.get_tracking(code) if self.post_exit_tracker else [],
        )

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_position_detail(self, code: str) -> PositionCardV2 | None:
        pool = self.stock_pool.get(code)
        if not pool:
            return None
        entry_reason = pool.get("added_reason") or "策略推荐"
        current_score = pool.get("current_score")
        score_str = str(current_score) if current_score is not None else "N/A"
        return PositionCardV2(
            code=code,
            name=pool.get("name", ""),
            entry_price=pool.get("entry_price", 0) or 0,
            current_price=pool.get("last_price", 0) or 0,
            pnl_pct=pool.get("unrealized_pnl_pct", 0) or 0,
            holding_days=pool.get("holding_days", 0) or 0,
            stop_loss=pool.get("stop_loss", 0) or 0,
            take_profit=pool.get("take_profit", 0) or 0,
            score=pool.get("current_score", 0) or 0,
            entry_reason=pool.get("added_reason", ""),
            position_pct=pool.get("position_pct", 0) or 0,
            reason_chain=f"入场原因: {entry_reason} | 当前评分: {score_str}",
        )

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_position_value_curve(self, code: str) -> list[ValueCurvePoint]:
        pool = self.stock_pool.get(code)
        if not pool:
            return []
        rows = self.pnl_tracker.get_position_value_curve(code)
        return [
            ValueCurvePoint(date=r.get("date", ""), price=r.get("price", 0), pnl_pct=r.get("pnl_pct", 0))
            for r in rows
        ]

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_portfolio_value_curve(self, days: int = 60) -> list[PortfolioCurvePoint]:
        rows = self.pnl_tracker.get_portfolio_value_curve(days)
        return [
            PortfolioCurvePoint(
                date=r.get("date", ""),
                position_value=r.get("position_value", 0),
                nav=r.get("nav", 0),
                pnl_pct=r.get("pnl_pct", 0),
                benchmark_nav=r.get("benchmark_nav", 0),
            )
            for r in rows
        ]

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_monthly_stats(self) -> MonthlyStats:
        stats = self.execution_stats.get_monthly_stats()
        if stats:
            return MonthlyStats(
                win_rate=stats.get("win_rate", 0),
                avg_return=stats.get("avg_return", 0),
                exit_accuracy=stats.get("exit_accuracy", 0),
                total_trades=stats.get("total_trades", 0),
            )
        return MonthlyStats()

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_order_state(self, code: str) -> OrderState | None:
        pool = self.stock_pool.get(code)
        if not pool:
            return None
        actions = self.action_record.get_by_code(code, limit=10)
        state = "pending"
        if pool.get("status") == "active":
            state = "executed"
        elif pool.get("status") == "exit_pending":
            state = "pending"
        elif pool.get("status") == "exited":
            state = "executed"
        return OrderState(
            code=code,
            order_type="entry",
            state=state,
            requested_qty=pool.get("position_qty", 0) or 0,
            filled_qty=pool.get("position_qty", 0) or 0,
            requested_price=pool.get("entry_price", 0) or 0,
            filled_price=pool.get("entry_price", 0) or 0,
            created_at=pool.get("added_date", ""),
            reason=pool.get("added_reason", ""),
            state_history=[a.get("action_type", "") for a in actions],
        )

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_summary_card(self) -> StrategySummaryCard:
        from execution_layer.stats.display import build_summary_card
        stats = self.execution_stats.get_summary_stats()
        return build_summary_card(stats)

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_best_worst_trades(self, limit: int = 5) -> BestWorstTradeCard:
        return self.execution_stats.get_best_worst_trades(limit)

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_return_attribution(self) -> ReturnAttribution:
        return self.execution_stats.get_return_attribution()

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_risk_contributions(self) -> list[RiskContribution]:
        return self.execution_stats.get_risk_contributions()

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_drawdown_analysis(self) -> DrawdownAnalysis:
        return self.execution_stats.get_drawdown_analysis()

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_monthly_heatmap(self, year: int = 0) -> MonthlyHeatmap:
        return self.execution_stats.get_monthly_heatmap(year)

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_rolling_metrics(self) -> RollingMetrics:
        return self.execution_stats.get_rolling_metrics()

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_multi_risk_metrics(self) -> MultiRiskMetrics:
        return self.execution_stats.get_multi_risk_metrics()

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_intraday_stats(self) -> IntradayStatsCard:  # noqa: F821
        from common.utils import bj_now
        from execution_layer.stats.display import IntradayStatsCard
        active = self.stock_pool.get_by_status("active")
        pending = self.stock_pool.get_by_status("pending_entry")
        exit_pending = self.stock_pool.get_by_status("exit_pending")

        total_pnl = sum(item.get("unrealized_pnl_pct", 0) or 0 for item in active)
        best = max(active, key=lambda x: x.get("unrealized_pnl_pct", 0) or 0) if active else {}
        worst = min(active, key=lambda x: x.get("unrealized_pnl_pct", 0) or 0) if active else {}

        return IntradayStatsCard(
            time=bj_now(),
            active_count=len(active),
            total_unrealized_pnl=round(total_pnl, 2),
            today_pnl=round(total_pnl, 2),
            best_performer=best.get("code", ""),
            worst_performer=worst.get("code", ""),
            pending_entries=len(pending),
            pending_exits=len(exit_pending),
        )

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_risk_expectation(self) -> RiskExpectationCard:
        risk_stats = self.stock_pool.get_risk_stats()
        worst = risk_stats.worst
        avg_loss = risk_stats.avg_loss
        active = self.stock_pool.get_by_status("active")
        current_dd = 0.0
        if active:
            pnl_values = [p.get("unrealized_pnl_pct", 0) or 0 for p in active]
            current_dd = min(pnl_values)
        risk = "高" if abs(worst) > 15 else ("中" if abs(worst) > 8 else "低")
        return RiskExpectationCard(
            worst_case_drawdown=round(worst, 2),
            var_95=round(avg_loss, 2),
            max_drawdown=round(worst, 2),
            current_drawdown=round(current_dd, 2),
            risk_level=risk,
        )

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_weighted_avg_price(self, code: str) -> float:
        return self.stock_pool.get_weighted_avg_price(code)

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_daily_price_range(self, code: str, date: str = "") -> DailyPriceRange | None:
        from common.utils import bj_date
        date = date or bj_date()
        if not self.data_layer:
            return None
        try:
            history = self.data_layer.get_history(code, date, 1)
            if history and history.klines and len(history.klines) > 0:
                k = history.klines[-1]
                return DailyPriceRange(
                    code=code,
                    date=date,
                    open_price=k.open,
                    high_price=k.high,
                    low_price=k.low,
                    close_price=k.close,
                    change_pct=(k.close / k.open - 1) * 100 if k.open else 0,
                )
        except Exception:
            pass
        return None

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_position_reason_chain(self, code: str) -> str:
        """持仓原因叙事链 — 为什么进来 → 发生了什么 → 为什么持有"""
        pool = self.stock_pool.get(code)
        if not pool:
            return ""
        parts = []
        entry_reason = pool.get("added_reason") or "策略推荐"
        parts.append(f"入场原因: {entry_reason}")
        if pool.get("status") == "active":
            score = pool.get("current_score") or 0
            days = pool.get("holding_days", 0) or 0
            pnl = pool.get("unrealized_pnl_pct", 0) or 0
            parts.append(f"持有 {days} 天, 浮盈 {pnl:+.1f}%, 评分 {score:.2f}")
            actions = self.action_record.get_by_code(code, limit=5)
            for a in actions:
                at = a.get("action_type", "")
                if at in ("adjust_stop_loss", "adjust_take_profit", "pnl_change"):
                    parts.append(f"{a.get('reason', '')}")
        elif pool.get("status") == "exited":
            exit_reason = pool.get("exit_reason", "")
            total_return = pool.get("total_return_pct", 0) or 0
            parts.append(f"退出原因: {exit_reason}, 收益 {total_return:+.1f}%")
        return " | ".join(parts)

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_volatility_explanation(self, code: str, change_pct: float) -> str:
        """波动解释 — 跌超3%时给出多维度解释"""
        if abs(change_pct) < 3:
            return ""
        # 获取大盘同期表现
        market_change = 0.0
        if self.data_layer:
            try:
                snap = self.data_layer.snapshot(bj_date())
                if snap and snap.klines:
                    changes = [k.change_pct for k in snap.klines if hasattr(k, "change_pct")]
                    if changes:
                        market_change = sum(changes) / len(changes)
            except Exception:
                pass
        parts = []
        if change_pct < 0:
            parts.append(f"今日下跌 {abs(change_pct):.1f}%")
            if market_change < -1:
                parts.append("大盘整体走弱")
            elif market_change > 0:
                parts.append("个股弱于大盘")
            else:
                parts.append("震荡市中个股调整")
        else:
            parts.append(f"今日上涨 {change_pct:.1f}%")
        pool = self.stock_pool.get(code)
        if pool:
            score = pool.get("current_score") or 0
            if score < 0.3:
                parts.append("评分偏低，关注止损")
            elif score > 0.7:
                parts.append("评分较高，继续持有")
        return " | ".join(parts)

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_trade_grouping(self) -> TradeGrouping:
        """交易分组 — 按状态/策略分组"""
        all_pool = self.stock_pool.get_all()
        by_status: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for item in all_pool:
            status = item.get("status", "unknown")
            source = item.get("source", "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            by_source[source] = by_source.get(source, 0) + 1
        return TradeGrouping(by_status=by_status, by_source=by_source)

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_net_cash_flow(self) -> list[CashFlowRow]:
        """净现金流 — 每笔操作的资金影响"""
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT action_type, COUNT(*) as cnt, SUM(COALESCE(new_value, 0)) as total "
            "FROM action_records GROUP BY action_type ORDER BY cnt DESC"
        ).fetchall()
        return [CashFlowRow(action=r["action_type"], count=r["cnt"], total_value=r["total"] or 0.0) for r in rows]

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_win_loss_classification(self) -> TradeClassification:  # noqa: F821
        """胜率/持仓时长分类"""
        from execution_layer.stats.display import TradeClassification
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT status, holding_days_actual, total_return_pct FROM stock_pool WHERE status = 'exited'"
        ).fetchall()
        short, medium, long_t = [], [], []
        for r in rows:
            days = r["holding_days_actual"] or 0
            ret = r["total_return_pct"] or 0
            if days <= 5:
                short.append(ret)
            elif days <= 20:
                medium.append(ret)
            else:
                long_t.append(ret)
        def _stats(vals):
            if not vals:
                return 0.0, 0.0
            wins = sum(1 for v in vals if v > 0)
            return wins / len(vals), sum(vals) / len(vals)
        sw, sr = _stats(short)
        mw, mr = _stats(medium)
        lw, lr = _stats(long_t)
        return TradeClassification(
            short_term=len(short), medium_term=len(medium), long_term=len(long_t),
            short_win_rate=round(sw, 4), medium_win_rate=round(mw, 4), long_win_rate=round(lw, 4),
            avg_short_return=round(sr, 4), avg_medium_return=round(mr, 4), avg_long_return=round(lr, 4),
        )

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=30)
    def daily_update(self, klines: list | None = None):
        from config_layer import cfg_get
        expire_days = cfg_get("execution.watch_expire_days", 7)
        expired = self.stock_pool.expire_stale_watch(expire_days)
        if expired:
            logger.info(f"Watch pool expired: {len(expired)} stocks")
        # S5: 入场区间过期的 pending 作废回观察池 (写侧 valid_until 算术已修,
        # 存量过期单不再每轮重评刷 entry_blocks)
        expired_pending = self.stock_pool.expire_stale_pending()
        if expired_pending:
            logger.info(f"Pending entries expired: {len(expired_pending)} stocks")
        # F4.6: 消费已批准的人工审核记录 (buy → pending_entry)
        plan_manager = getattr(self, "plan_manager", None)
        if plan_manager:
            plan_manager.consume_approved()
        # S4 通电: 持有期复评状态机 (05底稿项6, 建了没通电) — 到期待复评
        # (持有满 min_hold_days=5 交易日) 走 assess_heat 五路信号复评:
        # 续持→valid_until 延长; 退出→mark_exit_trigger (下方 _settle_pending_exits
        # 结算); pending_entry 超期→作废回 watch。策略A "5~10 交易日必须复评" 落地,
        # L5 30天强退保留为兜底。延迟 import 免 execution→strategy 顶层依赖; 失败不阻断。
        try:
            from strategy_layer.lifecycle.heat import assess_heat
            from strategy_layer.lifecycle.holding import HoldingStateMachine
            sm = HoldingStateMachine(self.db, self.stock_pool, heat_assessor=assess_heat)
            review = sm.run(as_of=bj_date())
            if review.get("due_for_review") or review.get("stale_flagged"):
                logger.info(f"Holding review: {review['due_for_review']} 复评, "
                            f"{review['stale_flagged']} 超期标记")
        except Exception as e:  # noqa: BLE001 — 状态机失败不拖垮 daily_update 主路径
            logger.warning(f"持有期复评状态机运行失败 (non-blocking): {e}")
        self.pnl_tracker.daily_update(klines)
        self._settle_pending_exits()
        self.execution_stats.save_daily(bj_date())
        if self.post_exit_tracker:
            self.post_exit_tracker.check()

    def _alert_settlement_deferred(self, pending: list, missing: list,
                                  today: str, reason: str) -> None:
        """AUD-005 病根③ (P0): 结算顺延必须告警。

        旧实现顺延只写 logger.warning + action_records —— alerts 表**全历史零结算类
        告警**。2026-09-08~11 连续 4 个交易日整批顺延、22 只卡死 exit_pending、
        active=0、零平仓动作, 全程无人知。这是 P0 能潜伏 4 个交易日的制度性原因。

        升级规则: 本批有滞留超过审计日的 → critical (T+1 语义已破), 否则 warning。
        去重由 AlertManager 的同名 open 抑制负责 (持续期一条 open, 不每日轰炸)。
        fail-safe: 告警失败只 warning, 不阻断顺延返回路径。
        """
        try:
            from ops_layer.alert_manager import AlertManager
            from ops_layer.core.models import Alert

            stamps = [str(i.get("trigger_date") or i.get("updated_at") or "")[:10]
                      for i in pending]
            stamps = [x for x in stamps if x]
            oldest = min(stamps) if stamps else ""
            stale = bool(oldest and oldest < today)
            AlertManager(self.db).send(Alert(
                severity="critical" if stale else "warning",
                layer="execution",
                title="T+1 结算顺延",
                detail=(
                    f"{len(pending)} 只 exit_pending 未能结算: {reason}"
                    + (f"; 最早滞留自 {oldest} (T+1 语义已破, 每多顺延一日"
                       f"结算价偏离触发日越远)" if stale else "")
                ),
                action=("核对当日 K 线落库进度 (ddp 15:30 / kline 17:40); "
                        "K 线到位后需**补跑**结算 —— 顺延不会自动补跑, 积压会累加"),
            ))
        except Exception as e:
            logger.warning(f"结算顺延告警发送失败 (fail-safe, 不阻断顺延): {e}")

    def _settle_pending_exits(self):
        """T+1 结算 (F4.4 唯一结算入口): 当日收盘价结算 exit_pending → exited。

        盘中只标记触发 (mark_exit_trigger), 不结算; 此处含费用计算并回填
        total_fees/net_return/net_return_pct (F4.1 新列)。
        EX-10: 结算前校验当日 K 线已入库 (查 daily_klines 当日行数), 缺则顺延 + 告警,
        不与 15:30 数据管线竞争。
        """
        from execution_layer.core.constraint_checker import calculate_fees
        today = bj_date()
        pending = self.stock_pool.get_by_status("exit_pending")
        if not pending:
            return
        conn = self.db.get_connection()

        # AUD-005 病根① (P0, 2026-09-14 审计): 守卫口径必须与结算口径同源。
        # 旧 EX-10 问的是「今天**有没有任何一行** K 线」(全局 COUNT), 而结算做的是
        # **逐股**查价 —— ddp 部分落库时守卫放行、逐股落空, 于是掉进病根② 伪造 0% 收益。
        # 2026-09-07/08 实证: 全局有 25,876~27,420 行, 这 18 只一行没有。
        # 改为只查本批 pending 这些 code 的当日覆盖; 缺任一只即**整批**顺延
        # (不部分结算 —— 同批两种口径会让当日统计口径不一致)。
        # 顺带: 逐股查价从 N 次查询收敛为 1 次批查 (原 N+1)。
        codes = [item["code"] for item in pending]
        closes: dict[str, float] = {}
        try:
            _ph = ",".join("?" * len(codes))
            for _c, _close in conn.execute(
                f"SELECT code, close FROM daily_klines WHERE date = ? AND code IN ({_ph})",
                (today, *codes),
            ):
                if _close and _close > 0:
                    closes[_c] = _close
        except sqlite3.Error as e:
            # 查询失败保守顺延 (宁可不结算, 也不拿不完整数据写永久账)
            logger.warning(f"T+1 结算: 当日收盘价覆盖查询失败 ({e}), 保守顺延")
        missing = [c for c in codes if c not in closes]

        if missing:
            reason = (
                f"当日收盘价未覆盖本批 {len(missing)}/{len(codes)} 只 (date={today}, "
                f"缺: {', '.join(missing[:8])}{' …' if len(missing) > 8 else ''})"
            )
            logger.warning(f"T+1 结算顺延: {reason}; {len(pending)} 只持仓顺延结算")
            for item in pending:
                self.action_record.write(
                    item["code"], "skip_exit", f"结算顺延: {reason}", "execution",
                )
            self._alert_settlement_deferred(pending, missing, today, reason)
            return

        track_exit_source = False
        try:
            from config_layer import cfg_get
            track_exit_source = bool(cfg_get("execution.track_exit_source", False))
        except Exception:
            track_exit_source = False

        settled = 0
        for item in pending:
            code = item["code"]
            # AUD-005 病根② (P0): 结算价只认当日真收盘价, **绝不回落存量
            # item['last_price']** —— 那个存量值恰等于 entry_price (生产 22 只
            # exit_pending 实测全中), 回落即写出 total_return_pct=0.00 的伪造账:
            # 2026-09-07/08 共 18 笔记 0.00%, 按当日真 close 复算真值合计 +46.38%,
            # 而 execution_stats 因此记成 25 连败零胜。伪造值与真值在库里长得一模一样。
            # 上方覆盖校验已保证 closes 命中; 此处只留防御分支, 跳过而非编造。
            last_price = closes.get(code, 0.0)
            if last_price <= 0:
                logger.warning(
                    f"T+1 结算跳过 {code}: 无当日收盘价 (不回落存量价, 禁伪造)")
                continue
            entry_price = item.get("entry_price") or 0
            position_qty = item.get("position_qty") or 0
            total_return = (last_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
            proceeds = position_qty * last_price
            # 费用: 卖出侧 (印花税+佣金+过户费), 净收益回填 F4.1 新列
            fees = calculate_fees(last_price, position_qty, is_buy=False) if position_qty > 0 else {"total": 0.0, "net_amount": proceeds}
            entry_cost = position_qty * entry_price
            net_return = fees["net_amount"] - entry_cost
            net_return_pct = net_return / entry_cost * 100 if entry_cost > 0 else 0
            exit_source_val = item.get("exit_source") or ("engine" if track_exit_source else None)
            if track_exit_source:
                cursor = conn.execute("""
                    UPDATE stock_pool SET
                        status='exited', exit_price=?, exit_date=?,
                        exit_reason=?, exit_reason_detail=?, exit_source=?,
                        total_return_pct=?, holding_days_actual=?,
                        total_fees=?, net_return=?, net_return_pct=?,
                        exit_low=?, last_price=?, last_price_date=?,
                        updated_at=datetime('now','+8 hours')
                    WHERE code=? AND status='exit_pending'
                """, (last_price, today, item.get("exit_reason") or "T+1确认卖出",
                      "daily_update T+1 结算", exit_source_val, round(total_return, 2),
                      item.get("holding_days", 0) or 0,
                      fees["total"], round(net_return, 2), round(net_return_pct, 2),
                      last_price, last_price, today, code))
            else:
                cursor = conn.execute("""
                    UPDATE stock_pool SET
                        status='exited', exit_price=?, exit_date=?,
                        exit_reason=?, exit_reason_detail=?,
                        total_return_pct=?, holding_days_actual=?,
                        total_fees=?, net_return=?, net_return_pct=?,
                        exit_low=?, last_price=?, last_price_date=?,
                        updated_at=datetime('now','+8 hours')
                    WHERE code=? AND status='exit_pending'
                """, (last_price, today, item.get("exit_reason") or "T+1确认卖出",
                      "daily_update T+1 结算", round(total_return, 2),
                      item.get("holding_days", 0) or 0,
                      fees["total"], round(net_return, 2), round(net_return_pct, 2),
                      last_price, last_price, today, code))
            if cursor.rowcount != 1:
                # 零行 UPDATE 仍开启写事务；释放锁后再跳过已消费的旧快照。
                conn.rollback()
                logger.info("T+1 结算跳过 %s: exit_pending 状态已被消费", code)
                continue
            self.action_record.write(
                code, "exit", item.get("exit_reason") or "T+1确认卖出", "execution",
                price=last_price, new_value=proceeds,
                action_detail=f"qty={position_qty},price={last_price:.2f},return={total_return:.2f}%,fees={fees['total']:.2f}",
            )
            # F4.5: 结算完成 → 创建 T+5/T+10/T+20 退出后跟踪 (INSERT OR IGNORE 幂等)
            tracker = getattr(self, "post_exit_tracker", None)
            if tracker:
                tracker.create_tracking(
                    code, today, last_price, entry_price, round(total_return, 2),
                    item.get("exit_reason") or "T+1确认卖出")
            settled += 1
        if settled:
            conn.commit()
            logger.info(f"T+1 结算完成: {settled}/{len(pending)} 只 exit_pending → exited")

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=30)
    def intraday_scan(self):
        self.intraday_monitor._scan()

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=10)
    def run_analyzer(self, name: str) -> AnalyzerResult:
        if name in self.analyzers:
            result = self.analyzers[name].analyze()
            return AnalyzerResult(
                name=name,
                sqn=result.get("sqn", 0),
                quality=result.get("quality", ""),
                num_trades=result.get("num_trades", 0),
                avg_return=result.get("avg_return", 0),
                win_rate=result.get("win_rate", 0),
                detail={k: v for k, v in result.items() if k not in ("sqn", "quality", "num_trades", "avg_return", "win_rate")},
            )
        return AnalyzerResult(name=name, detail={"error": f"Analyzer '{name}' not found"})

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def list_analyzers(self) -> list[str]:
        return list(self.analyzers.keys())

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_today_actions(self) -> ActionFeed:  # noqa: F821
        from execution_layer.stats.display import ActionFeed
        actions = self.action_record.get_today()
        return ActionFeed(actions=actions)

    @catch_quantstar_error(layer_name="execution")
    @with_timeout(timeout_s=5)
    def get_exit_reviews(self, limit: int = 10) -> list[ExitReview]:
        exited = self.stock_pool.get_by_status("exited")[:limit]
        results = []
        for item in exited:
            post = self.post_exit_tracker.get_tracking(item["code"]) if self.post_exit_tracker else []
            results.append(ExitReview(
                code=item["code"],
                name=item.get("name", ""),
                entry_price=item.get("entry_price", 0) or 0,
                exit_price=item.get("exit_price", 0) or 0,
                total_return_pct=item.get("total_return_pct", 0) or 0,
                holding_days=item.get("holding_days_actual", 0) or 0,
                exit_reason=item.get("exit_reason", ""),
                post_exit_t=post,
            ))
        return results

    def health(self):
        from ops_layer.core.models import HealthCheck, LayerHealth
        checks = []

        # 1. Database connectivity
        try:
            conn = self.db.get_connection()
            conn.execute("SELECT 1").fetchone()
            checks.append(HealthCheck(name="database", status="pass", detail="数据库连接正常"))
        except Exception as e:
            checks.append(HealthCheck(name="database", status="fail", detail=f"数据库连接失败: {e}"))

        # 2. Stock pool status
        try:
            counts = self.stock_pool.count_by_status()
            total = sum(counts.values())
            active = counts.get("active", 0)
            watch = counts.get("watch", 0)
            pending_entry = counts.get("pending_entry", 0)
            pool_status = "pass" if total > 0 else "warn"
            checks.append(HealthCheck(name="stock_pool", status=pool_status,
                detail=f"总计 {total} 只 | 观察 {watch} | 待入 {pending_entry} | 持仓 {active}",
                metric=float(active)))
        except Exception as e:
            checks.append(HealthCheck(name="stock_pool", status="fail", detail=f"股票池检查失败: {e}"))

        # 3. Intraday monitor status
        try:
            monitor = getattr(self, "intraday_monitor", None)
            monitor_running = getattr(monitor, "_running", False)
            checks.append(HealthCheck(name="intraday_monitor", status="pass" if monitor_running else "warn",
                detail="盘中监控运行中" if monitor_running else "盘中监控未启动"))
        except Exception as e:
            checks.append(HealthCheck(name="intraday_monitor", status="warn", detail=f"监控检查失败: {e}"))

        # Overall
        status = "green"
        for c in checks:
            if c.status == "fail":
                status = "red"
                break
            elif c.status == "warn":
                status = "yellow"
        return LayerHealth(layer="execution", status=status, checks=checks,
            summary=f"Execution Layer {'正常' if status == 'green' else '需关注'}")

    def shutdown(self, timeout: float = 10.0):
        if self.intraday_monitor:
            self.intraday_monitor.stop()
        pending = self.stock_pool.get_by_status("pending_entry", "exit_pending")
        logger.info(f"Execution Layer 关闭, 未处理操作: {len(pending)}")
        return True

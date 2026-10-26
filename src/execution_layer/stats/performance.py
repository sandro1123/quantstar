"""执行统计 — 周期统计 + 最佳/最差交易 + 回撤分析"""
from common.database import Database
from common.logging import get_logger
from execution_layer.stats.display import (
    BestWorstTradeCard,
    DrawdownAnalysis,
    MonthlyHeatmap,
    MultiRiskMetrics,
    ReturnAttribution,
    RiskContribution,
    RollingMetrics,
    TradeClassification,
    TradeHighlight,
)

logger = get_logger("execution.stats")


class ExecutionStats:
    def __init__(self, db: Database):
        self.db = db
        self._ensure_table()

    def _ensure_table(self):
        from execution_layer.tables import EXECUTION_STATS_DDL
        conn = self.db.get_connection()
        conn.execute(EXECUTION_STATS_DDL)
        conn.commit()

    def calc_daily(self, date: str) -> dict:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT action_type, COUNT(*) as cnt FROM action_records WHERE created_at LIKE ? GROUP BY action_type",
            (f"{date}%",),
        ).fetchall()
        return {r["action_type"]: r["cnt"] for r in rows}

    def reconcile_exit_ledger(self, date: str | None = None) -> dict:
        """统计漏账对账 —— **只报不改**（统筹 sm-f8184cb7 裁 GO）。

        `save_daily` 的作用域是 `status='exited' AND exit_date=?`：它认的是
        **当前状态**。而 `stock_pool` 是 code 主键单行表，「当前状态」与
        「历史了结账」挤在同一行 —— 一只票了结后被重新加回观察池，它那笔
        真实交易就跟着行一起流转走，**永远进不了统计面**。

        生产铁证 (2026-09-15)：`execution_stats` 09-08 total_trades=9，而
        `exit_date='2026-09-08'` 的行里有 1 行是 `watch`（600152，09-09 被打回）。
        那笔真实了结 +0.62% 从未入账；AUD-005 把真值回填进那一行了，统计面
        依然看不见 —— 那一行的回填等于白做。

        本函数不改任何东西：漏账显形归显形，补账与是否改 `save_daily` 口径
        各走各的决策（同 `restore_coverage` 的路子——让缺口可见，不擅自扩语义）。

        判据是 **`status != 'exited'`（补集）而非枚举具体活态** ——
        按枚举圈会随状态机演进漏掉新态；这是「按状态圈会漏、按补集/值形态圈
        才稳」的同一条纪律。

        date 给定 -> 当日报告；date=None -> 全量普查（含 by_date 分日账）。
        """
        conn = self.db.get_connection()
        where = "exit_date IS NOT NULL" + (" AND exit_date = ?" if date else "")
        args = (date,) if date else ()
        rows = conn.execute(
            f"SELECT code, status, exit_date, exit_reason, entry_price, "
            f"exit_price, total_return_pct FROM stock_pool WHERE {where} "
            f"ORDER BY exit_date, code", args).fetchall()

        by_date: dict[str, dict] = {}
        orphans = []
        for r in rows:
            d = r["exit_date"]
            b = by_date.setdefault(d, {"counted": 0, "orphaned": 0})
            if r["status"] == "exited":
                b["counted"] += 1
            else:
                b["orphaned"] += 1
                orphans.append({
                    "code": r["code"], "status": r["status"], "exit_date": d,
                    "exit_reason": r["exit_reason"], "entry_price": r["entry_price"],
                    "exit_price": r["exit_price"],
                    "total_return_pct": r["total_return_pct"],
                })
        counted = sum(b["counted"] for b in by_date.values())
        orphaned = len(orphans)
        if orphaned:
            logger.warning(
                f"统计漏账对账{' ' + date if date else '(全量)'}: "
                f"{orphaned} 笔了结因 status 流转掉出 save_daily 作用域 -> "
                f"{[o['code'] for o in orphans]} (只报不改; 补账需人裁)")
        return {"date": date, "counted": counted, "orphaned": orphaned,
                "orphans": orphans, "by_date": by_date}

    def save_daily(self, date: str):
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT action_type, COUNT(*) as cnt FROM action_records WHERE created_at LIKE ? GROUP BY action_type",
            (f"{date}%",),
        ).fetchall()
        action_counts = {r["action_type"]: r["cnt"] for r in rows}
        # W2-1 丙案 (2026-09-20): executed_entries 改读 stock_pool.entry_date,
        # 不再读 action_records["entry"]。双病:
        #   ①源错配: action_records 由 paper_broker 在 16:35 写 (与 entry_date 同事务),
        #     而本方法挂在 daily_update (cron 16:16) → 跑时那批 entry 尚未落库 → 恒 0
        #     (生产实证 09-14~09-18: exec_stats.executed_entries=0 而 ar.entry 有 5~21 笔)
        #   ②同族: total_trades 读 stock_pool (本 run 已落定) 故一直准确 → 与之同源
        # ⚠️ 锚点适用窗口下界 = 2026-08-12: stock_pool.entry_date 与 action_records.entry
        #   从该日起逐日对拍一致 (17/17 天实证); 2026-07-24 及更早不一致 (A=0 vs C=3,
        #   更早入池路径未同步写 entry_date)。**不得据此口径回填 08-12 之前的日期**。
        # ⚠️ 不得加 status 限定: status 随持仓周期推进 (active→exited), 它是当前时点
        #   快照而非历史日事实 (全库 entry_date 非空行: active 21 / exited 109)。
        #   加 status='active' 会使历史日全部低估为 0 (14 天里 12 天实证)。
        executed_entries = conn.execute(
            "SELECT COUNT(*) FROM stock_pool WHERE entry_date = ?",
            (date,),
        ).fetchone()[0]
        # S1 口径修复: 累计快照→当日增量, 只数当日了结持仓, 行间可比
        # P0-3 (2026-09-28): 分母口径修正 — watch_expired（观察剔除, 从未买入）
        # 不进 total_trades。09-25 实证: 11 行 exited 全是 watch_expired
        # → total_trades=11（错分母）→ win_rate=0.0（真值但错分母）。
        # 排除条件 = exit_reason='watch_expired'（语义锚: 「没买过的观察剔除」），
        # 非 entry_price IS NULL（结果锚: 会误伤 add 后 exit 的既有测试夹具语义）。
        total_trades = conn.execute(
            "SELECT COUNT(*) FROM stock_pool WHERE status='exited' "
            "AND exit_date=? AND (exit_reason IS NULL OR exit_reason != 'watch_expired')",
            (date,),
        ).fetchone()[0]
        win_count = conn.execute(
            "SELECT COUNT(*) FROM stock_pool WHERE status='exited' "
            "AND exit_date=? AND (exit_reason IS NULL OR exit_reason != 'watch_expired') "
            "AND total_return_pct > 0",
            (date,),
        ).fetchone()[0]
        loss_count = total_trades - win_count
        win_rate = round(win_count / max(total_trades, 1), 4)
        exits = conn.execute(
            "SELECT COUNT(*) as cnt, exit_reason FROM stock_pool "
            "WHERE status='exited' AND exit_date=? AND exit_reason IS NOT NULL "
            "GROUP BY exit_reason",
            (date,),
        ).fetchall()
        stop_loss = sum(r["cnt"] for r in exits if "止损" in r["exit_reason"])
        take_profit = sum(r["cnt"] for r in exits if "止盈" in r["exit_reason"])
        quality = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(quality, 'neutral') as q "
            "FROM post_exit_tracking WHERE created_at LIKE ? GROUP BY q",
            (f"{date}%",),
        ).fetchall()
        correct = sum(r["cnt"] for r in quality if r["q"] == "correct")
        neutral = sum(r["cnt"] for r in quality if r["q"] == "neutral")
        wrong = sum(r["cnt"] for r in quality if r["q"] == "wrong")
        total_q = correct + neutral + wrong or 1
        # 幂等: 同日重跑先删旧行再插, 避免 cron 重试产生重复统计行
        conn.execute(
            "DELETE FROM execution_stats WHERE period='daily' AND period_start=? AND period_end=?",
            (date, date),
        )
        conn.execute(
            "INSERT INTO execution_stats (period, period_start, period_end, "
            "total_recommendations, executed_entries, entry_rate, "
            "total_trades, win_count, loss_count, win_rate, "
            "stop_loss_count, take_profit_count, "
            "correct_exits, neutral_exits, wrong_exits, exit_accuracy) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("daily", date, date,
             # S1 口径修复: 推荐数=set_entry_range(买入推荐设入场区间);
             # 成交数=stock_pool.entry_date 计数 (W2-1 丙案换源, 见上); 旧口径误用 entry
             # 当分母、buy 当分子, 而 DDL 词表无 'buy' → entry_rate 恒 0
             action_counts.get("set_entry_range", 0), executed_entries,
             round(executed_entries
                   / max(action_counts.get("set_entry_range", 0), 1), 4),
             total_trades, win_count, loss_count, win_rate,
             stop_loss, take_profit,
             correct, neutral, wrong,
             round(correct / total_q, 4)),
        )
        conn.commit()

    def get_monthly_stats(self) -> dict:
        """月度统计"""
        conn = self.db.get_connection()
        from common.utils import bj_date
        # AUD-006: 旧实现算了 today[:7] 却把它当弃值表达式丢掉, 三条 SQL 无任何
        # 月份 WHERE ——「月度统计」实为全历史统计。死语句即"本想接线没接"的铁证。
        month = bj_date()[:7]
        like = f"{month}-%"
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM stock_pool "
            "WHERE status='exited' AND exit_date LIKE ?", (like,)
        ).fetchone()["cnt"]
        win = conn.execute(
            "SELECT COUNT(*) as cnt FROM stock_pool "
            "WHERE status='exited' AND exit_date LIKE ? "
            "AND (exit_reason IS NULL OR exit_reason != 'watch_expired') "
            "AND total_return_pct > 0", (like,)
        ).fetchone()["cnt"]
        win_rate = round(win / total, 4) if total > 0 else 0.0
        rows = conn.execute(
            "SELECT total_return_pct FROM stock_pool WHERE status='exited' "
            "AND exit_date LIKE ? AND (exit_reason IS NULL OR exit_reason != 'watch_expired') "
            "AND total_return_pct IS NOT NULL", (like,)
        ).fetchall()
        returns = [r["total_return_pct"] for r in rows]
        avg_return = round(sum(returns) / len(returns), 4) if returns else 0.0
        return {
            "win_rate": win_rate,
            "avg_return": avg_return,
            "total_trades": total,
        }

    def get_summary_stats(self) -> dict:
        """汇总统计"""
        conn = self.db.get_connection()
        from common.utils import bj_date
        total = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(AVG(total_return_pct), 0) as avg_ret, "
            "COALESCE(SUM(CASE WHEN total_return_pct > 0 THEN 1 ELSE 0 END), 0) as wins, "
            "COALESCE(SUM(CASE WHEN total_return_pct < 0 THEN 1 ELSE 0 END), 0) as losses "
            "FROM stock_pool WHERE status='exited'"
        ).fetchone()
        total_trades = total["cnt"] or 0
        wins = total["wins"] or 0
        total["losses"] or 0
        win_rate = round(wins / total_trades, 4) if total_trades > 0 else 0.0
        avg_return = round(total["avg_ret"] or 0, 4)
        active = conn.execute(
            "SELECT COUNT(*) as cnt FROM stock_pool WHERE status='active'"
        ).fetchone()["cnt"]
        return {
            "total_return_pct": avg_return,
            "win_rate": win_rate,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "total_trades": total_trades,
            "active_positions": active,
            "date": bj_date(),
        }

    def get_best_worst_trades(self, limit: int = 5) -> BestWorstTradeCard:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT code, entry_price, exit_price, total_return_pct, exit_reason "
            "FROM stock_pool WHERE status='exited' AND total_return_pct IS NOT NULL "
            "ORDER BY total_return_pct DESC LIMIT ?", (limit,)
        ).fetchall()
        best = [TradeHighlight(code=r["code"], action="sell", price=r["exit_price"] or 0,
                               pnl_pct=r["total_return_pct"] or 0, reason=r["exit_reason"] or "") for r in rows]
        rows = conn.execute(
            "SELECT code, entry_price, exit_price, total_return_pct, exit_reason "
            "FROM stock_pool WHERE status='exited' AND total_return_pct IS NOT NULL "
            "ORDER BY total_return_pct ASC LIMIT ?", (limit,)
        ).fetchall()
        worst = [TradeHighlight(code=r["code"], action="sell", price=r["exit_price"] or 0,
                                pnl_pct=r["total_return_pct"] or 0, reason=r["exit_reason"] or "") for r in rows]
        return BestWorstTradeCard(best=best, worst=worst)

    def get_trade_classification(self) -> TradeClassification:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT holding_days_actual, total_return_pct FROM stock_pool "
            "WHERE status='exited' AND holding_days_actual IS NOT NULL"
        ).fetchall()
        short, med, long_t = [], [], []
        for r in rows:
            days = r["holding_days_actual"]
            ret = r["total_return_pct"] or 0
            if days < 5:
                short.append(ret)
            elif days < 20:
                med.append(ret)
            else:
                long_t.append(ret)

        def win_rate(lst):
            return sum(1 for r in lst if r > 0) / len(lst) if lst else 0

        return TradeClassification(
            short_term=len(short), medium_term=len(med), long_term=len(long_t),
            short_win_rate=round(win_rate(short), 4),
            medium_win_rate=round(win_rate(med), 4),
            long_win_rate=round(win_rate(long_t), 4),
            avg_short_return=round(sum(short) / len(short), 2) if short else 0,
            avg_medium_return=round(sum(med) / len(med), 2) if med else 0,
            avg_long_return=round(sum(long_t) / len(long_t), 2) if long_t else 0,
        )

    def get_return_attribution(self) -> ReturnAttribution:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT total_return_pct FROM stock_pool WHERE status='exited' AND total_return_pct IS NOT NULL"
        ).fetchall()
        returns = [r["total_return_pct"] for r in rows]
        if not returns:
            return ReturnAttribution()
        total = sum(returns)
        positive = [r for r in returns if r > 0]
        negative = [r for r in returns if r < 0]
        stock_sel = sum(positive) / len(returns) if positive else 0
        return ReturnAttribution(
            total_return_pct=round(total, 2),
            stock_selection=round(stock_sel, 2),
            timing=round(sum(negative) / len(returns) if negative else 0, 2),
            allocation=0.0,
            residual=0.0,
            num_positive=len(positive),
            num_negative=len(negative),
        )

    def get_risk_contributions(self) -> list[RiskContribution]:
        active = self.db.get_connection().execute(
            "SELECT * FROM stock_pool WHERE status='active'"
        ).fetchall()
        active = [dict(r) for r in active]
        if not active:
            return []
        total_risk = sum(
            abs(p.get("unrealized_pnl_pct", 0) or 0) * (p.get("position_pct", 0) or 0)
            for p in active
        )
        if total_risk == 0:
            return []
        result = []
        for p in active:
            weight = p.get("position_pct", 0) or 0
            pnl = p.get("unrealized_pnl_pct", 0) or 0
            risk_contrib = abs(pnl) * weight / total_risk * 100
            result.append(RiskContribution(
                code=p["code"],
                name=p.get("name", ""),
                weight_pct=round(weight, 1),
                risk_contribution_pct=round(risk_contrib, 1),
                unrealized_pnl_pct=round(pnl, 2),
            ))
        return sorted(result, key=lambda x: x.risk_contribution_pct, reverse=True)

    def get_monthly_heatmap(self, year: int = 0) -> MonthlyHeatmap:
        from common.utils import bj_date
        conn = self.db.get_connection()
        target_year = year or int(bj_date()[:4])
        rows = conn.execute(
            "SELECT exit_date, total_return_pct FROM stock_pool "
            "WHERE status='exited' AND exit_date LIKE ? AND total_return_pct IS NOT NULL",
            (f"{target_year}%",)
        ).fetchall()
        monthly = {m: {"returns": [], "wins": 0} for m in range(1, 13)}
        for r in rows:
            try:
                m = int(r["exit_date"][5:7])
                ret = r["total_return_pct"] or 0
                monthly[m]["returns"].append(ret)
                if ret > 0:
                    monthly[m]["wins"] += 1
            except (ValueError, IndexError):
                pass
        months = []
        for m in range(1, 13):
            data = monthly[m]
            total = len(data["returns"])
            months.append({
                "month": m,
                "return_pct": round(sum(data["returns"]), 2) if total else 0,
                "trade_count": total,
                "win_rate": round(data["wins"] / total, 2) if total else 0,
            })
        return MonthlyHeatmap(year=target_year, months=months)

    def get_drawdown_analysis(self) -> DrawdownAnalysis:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT total_return_pct, holding_days_actual, exit_date "
            "FROM stock_pool WHERE status='exited' AND total_return_pct IS NOT NULL "
            "ORDER BY exit_date"
        ).fetchall()
        max_dd = 0.0
        max_dd_days = 0
        dd_periods = []
        cumulative = 0.0
        peak = 0.0
        peak_date = ""
        in_drawdown = False
        dd_start = ""
        for r in rows:
            cumulative += r["total_return_pct"] or 0
            if cumulative > peak:
                peak = cumulative
                peak_date = r["exit_date"][:10] if r["exit_date"] else ""
                if in_drawdown:
                    dd_periods.append({
                        "start": dd_start,
                        "end": peak_date,
                        "depth": round(max_dd, 2),
                    })
                    in_drawdown = False
            dd = (peak - cumulative) / max(peak, 1) * 100 if cumulative < peak else 0
            if dd > max_dd:
                max_dd = dd
                max_dd_days = 0
            if dd > 0:
                max_dd_days += 1
                if not in_drawdown:
                    in_drawdown = True
                    dd_start = peak_date
        active = self.db.get_connection().execute(
            "SELECT code, unrealized_pnl_pct FROM stock_pool WHERE status='active'"
        ).fetchall()
        current_dd = min((p["unrealized_pnl_pct"] or 0) for p in active) if active else 0.0
        current_dd_days = sum(1 for r in rows if (r["total_return_pct"] or 0) < 0)  # simple proxy
        return DrawdownAnalysis(
            max_drawdown_pct=round(max_dd, 2),
            max_drawdown_days=max_dd_days,
            max_drawdown_recovery_days=0,
            current_drawdown_pct=round(current_dd, 2),
            current_drawdown_days=current_dd_days,
            drawdown_periods=dd_periods,
        )

    def get_rolling_metrics(self) -> RollingMetrics:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT total_return_pct FROM stock_pool WHERE status='exited' AND total_return_pct IS NOT NULL ORDER BY exit_date DESC"
        ).fetchall()
        returns = [r["total_return_pct"] for r in rows]
        r20 = returns[:20] if len(returns) >= 20 else returns
        r60 = returns[:60] if len(returns) >= 60 else returns

        def win_rate(r):
            return sum(1 for x in r if x > 0) / len(r) if r else 0

        def sharpe(r):
            import statistics
            if len(r) < 2:
                return 0
            avg = statistics.mean(r)
            std = statistics.stdev(r)
            return round(avg / std * (252 ** 0.5), 2) if std > 0 else 0

        return RollingMetrics(
            period_20d_return=round(sum(r20), 2),
            period_60d_return=round(sum(r60), 2),
            period_20d_win_rate=round(win_rate(r20), 4),
            period_60d_win_rate=round(win_rate(r60), 4),
            period_20d_sharpe=sharpe(r20),
            period_60d_sharpe=sharpe(r60),
        )

    def get_multi_risk_metrics(self) -> MultiRiskMetrics:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT total_return_pct FROM stock_pool WHERE status='exited' AND total_return_pct IS NOT NULL"
        ).fetchall()
        returns = [r["total_return_pct"] for r in rows]
        import statistics
        if len(returns) < 2:
            return MultiRiskMetrics()
        avg = statistics.mean(returns)
        std = statistics.stdev(returns)
        neg_returns = [r for r in returns if r < 0]
        downside_std = statistics.stdev(neg_returns) if len(neg_returns) > 1 else 0
        max_dd = min(returns) if returns else 0
        var_95 = sorted(returns)[int(len(returns) * 0.05)] if len(returns) >= 20 else min(returns) if returns else 0
        cvar = statistics.mean([r for r in returns if r <= var_95]) if any(r <= var_95 for r in returns) else 0
        return MultiRiskMetrics(
            std_dev_pct=round(std, 2),
            sharpe_ratio=round(avg / std * (252 ** 0.5), 2) if std > 0 else 0,
            sortino_ratio=round(avg / downside_std * (252 ** 0.5), 2) if downside_std > 0 else 0,
            calmar_ratio=round(avg / abs(max_dd), 2) if max_dd < 0 else 0,
            var_95_pct=round(var_95, 2),
            cvar_95_pct=round(cvar, 2),
            max_drawdown_pct=round(max_dd, 2),
            downside_std_pct=round(downside_std, 2),
        )

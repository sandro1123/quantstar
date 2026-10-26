"""P&L 跟踪 — 每日更新持仓浮盈"""

from common.database import Database
from common.logging import get_logger
from common.utils import bj_date
from execution_layer.core.action_record import ActionRecord

logger = get_logger("execution.pnl")


class PnLTracker:
    def __init__(self, db: Database, stock_pool, config=None):
        self.db = db
        self.stock_pool = stock_pool
        self.action_record = ActionRecord(db)
        # EX-1: initial_capital 改读 config (constraint_checker.py:36 同款先例);
        # config 缺省 (旧测试夹具) 回落 300000.0 与 execution.yaml 现值一致。
        cfg_get = (config.get if hasattr(config, "get") else lambda k, d=None: d) if config else (lambda k, d=None: d)
        self.initial_capital = float(cfg_get("execution.initial_capital", 300000.0))

    def daily_update(self, klines: list | None = None):
        today = bj_date()
        from common.calendar import TradingCalendar
        cal = TradingCalendar()
        active = self.stock_pool.get_by_status("active")
        total_pnl = 0.0
        conn = self.db.get_connection()
        for item in active:
            code = item["code"]
            entry_price = item["entry_price"] or 0
            close_price = 0
            if klines:
                for k in klines:
                    if hasattr(k, "code") and k.code == code:
                        close_price = k.close
                        break
                    elif isinstance(k, dict) and k.get("code") == code:
                        close_price = k.get("close", 0)
                        break
            if close_price == 0:
                try:
                    row = conn.execute(
                        "SELECT close FROM daily_klines WHERE code = ? AND date = ?",
                        (code, today)
                    ).fetchone()
                    if row:
                        close_price = row[0]
                except Exception:
                    pass
            if close_price == 0:
                continue
            pnl_pct = (close_price / entry_price - 1) * 100 if entry_price else 0
            # P1/M1: 盯市同点补写绝对值列 — (close - entry) * qty。
            # 此前只写 pct, unrealized_pnl 全 repo 零写入 (生产 active 仓恒 NULL)。
            pnl_abs = (close_price - entry_price) * (item.get("position_qty") or 0) if entry_price else 0.0
            prev_pnl = item.get("unrealized_pnl_pct", 0) or 0
            # 交易日才增加 holding_days
            prev_days = item.get("holding_days", 0) or 0
            new_days = prev_days + (1 if cal.is_trading_day(today) else 0)
            self.stock_pool.update(code,
                last_price=close_price, last_price_date=today,
                unrealized_pnl=pnl_abs,
                unrealized_pnl_pct=pnl_pct,
                holding_days=new_days,
            )
            if abs(pnl_pct - prev_pnl) > 5:
                self.action_record.write(code, "pnl_change", f"PnL 变动超5%: {prev_pnl:.1f}% → {pnl_pct:.1f}%",
                    previous_value=prev_pnl, new_value=pnl_pct)
            total_pnl += pnl_pct
        return total_pnl

    def get_pnl_trend(self, days: int = 20) -> list[dict]:
        conn = self.db.get_connection()
        latest = conn.execute("SELECT MAX(last_price_date) FROM stock_pool WHERE last_price_date IS NOT NULL").fetchone()
        end_date = latest[0] if latest and latest[0] else bj_date()
        from datetime import datetime, timedelta
        start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT last_price_date as date, SUM(unrealized_pnl_pct) as total_pnl "
            "FROM stock_pool WHERE last_price_date IS NOT NULL "
            "AND last_price_date >= ? AND last_price_date <= ? "
            "GROUP BY last_price_date ORDER BY last_price_date DESC",
            (start, end_date)
        ).fetchall()
        return [{"date": r["date"], "pnl_pct": r["total_pnl"] or 0.0} for r in rows]

    def get_position_value_curve(self, code: str) -> list[dict]:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT last_price_date as date, last_price, unrealized_pnl_pct "
            "FROM stock_pool WHERE code=? AND last_price_date IS NOT NULL "
            "ORDER BY last_price_date", (code,)
        ).fetchall()
        return [{"date": r["date"], "price": r["last_price"], "pnl_pct": r["unrealized_pnl_pct"] or 0} for r in rows]

    def get_portfolio_value_curve(self, days: int = 60) -> list[dict]:
        """获取组合价值曲线 (NAV 序列)。基准对比面走 benchmark_return 结算线,
        本方法不画基准 — PortfolioCurvePoint.benchmark_nav 为兼容保留的死字段。"""
        # 包A 钉 3 (统 筹 GO 升 第一 + 判例 #143): 值域枚举先于数值计算。
        # 生产 stock_pool.status 实测值域: ['exit_pending','exited','pending_entry','watch'],
        # 'active' 从 未 写 过 (T+1 当日 active 走 后 转 exit_pending 稳态 active=0)。
        # 不 覆 exit_pending+pending_entry 则 NAV 分子 漏 27.9 万 真实 持仓 —
        # 真 NAV(-4.14%) 与 假 NAV(-96.98%) 的 差 异 来 源 (audit 定谳 2026-10-03)。
        conn = self.db.get_connection()
        today = bj_date()
        from datetime import datetime, timedelta
        start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT last_price_date as date, "
            "SUM(last_price * position_qty) as position_value, "
            "SUM(unrealized_pnl_pct) as total_pnl "
                        # 包A (2026-10-04): status 筛选 覆 exit_pending+pending_entry (T+1 结算 后 active 全 转 exit_pending, stock_pool 真实 值域 不 含 active 长 稳态)
            "FROM stock_pool WHERE last_price_date >= ? AND last_price_date <= ? "
            "AND status IN ('active', 'exit_pending', 'pending_entry') "
            "GROUP BY last_price_date ORDER BY last_price_date",
            (start, today)
        ).fetchall()
        curve = []
        for r in rows:
            position_value = r["position_value"] or 0
            # EX-1: 全系统唯一资金基准 execution.initial_capital (D-24/D-26)
            initial_capital = self.initial_capital
            cash = self.stock_pool.get_available_cash(initial_capital) if hasattr(self.stock_pool, 'get_available_cash') else initial_capital
            nav = cash + position_value
            curve.append({
                "date": r["date"],
                "nav": round(nav, 2),
                "position_value": round(position_value, 2),
                "pnl_pct": round(r["total_pnl"] or 0, 2),
            })
        return curve

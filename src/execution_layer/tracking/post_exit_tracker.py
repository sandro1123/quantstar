"""退出后跟踪 — T+5/T+10/T+20 验证"""

from common.database import Database
from common.logging import get_logger
from common.utils import bj_date
from execution_layer.core.action_record import ActionRecord

logger = get_logger("execution.tracking")

# ---- D席 fix/post-exit-due-logging: 到期判定三态 (单一定义点) --------------
# 病理 (2026-09-16 定谳): _due_point_price 旧实现 `if len(rows) < need: return 0`
# 把**两种语义吞成同一个 0**: ①真·未到期 ②已到期但取不到数据 (K线断供/停牌)。
# 实案 9 条: exit_date=09-08 的 T+5, 市场日历已过 5 交易日 (09-15) 但个股当日
# K 线缺行 (仅 4 天) → 误判「未到期」→ 静默跳过 → 当天该做的检查被吞。
# 修法: 用**全市场交易日历**判「是否到期」(T+N 是日历概念, 市场开市即过一天),
# 个股 K 线只负责「取价」—— 把到期判定与取价解耦。
DUE_DONE = "DUE_DONE"                      # 到期且取到价 → 评估
DUE_SKIPPED_NO_DATA = "DUE_SKIPPED_NO_DATA"  # 市场已到期但个股取不到价 → 留痕+告警
NOT_DUE = "NOT_DUE"                        # 真·未到期 → 静默


class PostExitTracker:
    def __init__(self, db: Database, stock_pool):
        self.db = db
        self.stock_pool = stock_pool
        self.action_record = ActionRecord(db)
        # D席 fix/post-exit-due-logging: 最近一次 check() 的三态计数
        self.last_check_stats = {DUE_DONE: 0, DUE_SKIPPED_NO_DATA: 0, NOT_DUE: 0}

    def _ensure_table(self):
        from execution_layer.tables import EXIT_TRACKING_DDL
        conn = self.db.get_connection()
        conn.execute(EXIT_TRACKING_DDL)
        conn.commit()

    def create_tracking(self, code: str, exit_date: str, exit_price: float,
                        entry_price: float, total_return_pct: float, exit_reason: str):
        self._ensure_table()
        conn = self.db.get_connection()
        for point in ["T+5", "T+10", "T+20"]:
            conn.execute("""
                INSERT OR IGNORE INTO post_exit_tracking
                (code, exit_date, exit_price, entry_price, total_return_pct, exit_reason, check_point)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (code, exit_date, exit_price, entry_price, total_return_pct, exit_reason, point))
        conn.commit()
        self.action_record.write(code, "exit_tracking", f"创建退出后跟踪, 总收益:{total_return_pct:.1f}%",
            price=exit_price)

    # 检查点 → 退出后第 N 个交易日
    _CHECK_POINT_DAYS = {"T+5": 5, "T+10": 10, "T+20": 20}

    def check(self, klines_by_code: dict | None = None):
        """到期执行退出后检查, 填 current_price/price_change_since_exit/quality。

        - 显式传 klines_by_code: 立即评估全部未判定记录 (测试/手动复盘路径)
        - 未传 (daily_update 生产路径, F4.5): 自查 daily_klines, 仅当记录已过
          检查点交易日数 (T+5/T+10/T+20) 时评估, 取到期日收盘价 (缺失取最近)
        """
        self._ensure_table()
        conn = self.db.get_connection()
        today = bj_date()
        records = conn.execute(
            "SELECT * FROM post_exit_tracking WHERE quality IS NULL"
        ).fetchall()
        checked = 0
        # D席 fix/post-exit-due-logging: 三态计数 (供调用方/测试观测)
        stats = {DUE_DONE: 0, DUE_SKIPPED_NO_DATA: 0, NOT_DUE: 0}
        skipped = []  # (code, check_point, exit_date) — 留痕用
        for r in records:
            current_price = 0
            state = DUE_DONE
            if klines_by_code:
                klines = klines_by_code.get(r["code"], [])
                if klines:
                    k = klines[-1]
                    current_price = k.close if hasattr(k, "close") else k.get("close", 0)
                else:
                    state = DUE_SKIPPED_NO_DATA
            else:
                state, current_price = self._due_point_state(conn, r)
            if state == NOT_DUE:
                stats[NOT_DUE] += 1
                continue
            if state == DUE_SKIPPED_NO_DATA:
                stats[DUE_SKIPPED_NO_DATA] += 1
                skipped.append((r["code"], r["check_point"], r["exit_date"]))
                continue
            stats[DUE_DONE] += 1
            if current_price == 0:
                continue
            price_change = (current_price - r["exit_price"]) / r["exit_price"] if r["exit_price"] else 0
            if price_change < -0.05:
                quality = "correct"
            elif price_change > 0.05:
                quality = "wrong"
            else:
                quality = "neutral"
            conn.execute("""
                UPDATE post_exit_tracking SET
                check_date=?, current_price=?, price_change_since_exit=?, quality=?
                WHERE id=?
            """, (today, current_price, price_change, quality, r["id"]))
            self.action_record.write(r["code"], "exit_tracking_check",
                f"{r['check_point']} 检查质量:{quality}, 涨幅:{price_change*100:.1f}%",
                price=current_price)
            checked += 1
        conn.commit()
        self.last_check_stats = stats
        # D席 fix/post-exit-due-logging: 跳过必须留痕 (铁律#12 降级不得静默)
        # 旧实现零留痕零告警 → 每日 K 线断供都会静默吞掉当天该做的检查,
        # 且只有事后补算才看得出 (9 条实案即如此)。
        if skipped:
            self._alert_due_skipped(skipped)
        return checked

    def _alert_due_skipped(self, skipped: list) -> None:
        """到期但取不到价 → 告警 (fail-safe: 告警失败不炸主链)。"""
        codes = ",".join(sorted({c for c, _, _ in skipped}))
        detail = (f"{len(skipped)} 条检查点已到期但个股无 K 线数据: {codes}; "
                  f"明细=" + "; ".join(f"{c}/{p}(exit={d})" for c, p, d in skipped[:10]))
        logger.warning(f"post_exit_tracker 到期未判定: {detail}")
        try:
            from ops_layer.alert_manager import AlertManager
            AlertManager(self.db).create_alert(
                title="退出后检查点到期未判定 (K线数据缺失)",
                detail=detail, severity="warning", layer="execution")
        except Exception as e:
            logger.warning(f"到期未判定告警失败 (留痕已进日志): {e}")

    def _market_due_date(self, conn, exit_date: str, need: int):
        """用**全市场交易日历**算 exit_date 后第 need 个交易日 (不足返回 None)。

        日历概念: T+N 只问「市场开市了几个交易日」, 与该股有无 K 线无关 ——
        这正是把「到期判定」与「取价」解耦的关键 (旧实现两者被个股数据耦合)。
        """
        try:
            rows = conn.execute(
                "SELECT DISTINCT date FROM daily_klines WHERE date>? ORDER BY date",
                (exit_date,),
            ).fetchall()
        except Exception:
            return None
        if len(rows) < need:
            return None
        return rows[need - 1][0]

    def _due_point_state(self, conn, record):
        """检查点状态: (state, price)。

        DUE_DONE            = 市场到期 且 个股自身也过 need 交易日 且取到价
        DUE_SKIPPED_NO_DATA = 市场已到期但个股数据不足/取不到价 → price 0 (须留痕)
        NOT_DUE             = 市场未过 N 交易日 → price 0 (静默)

        到期判定用全市场日历 (与个股数据无关); 取价用个股 K 线。
        """
        need = self._CHECK_POINT_DAYS.get(record["check_point"], 0)
        if not need:
            return NOT_DUE, 0
        # ① 到期判定: 全市场日历 (解耦点)
        market_due = self._market_due_date(conn, record["exit_date"], need)
        if market_due is None:
            return NOT_DUE, 0
        # ② 取价: 个股 K 线须**自身也过 need 个交易日** (与市场日历对齐),
        #    否则是「市场到期但个股数据缺失」= 须留痕, 不得用旧价冒充到期价。
        try:
            own = conn.execute(
                "SELECT DISTINCT date FROM daily_klines WHERE code=? AND date>? ORDER BY date",
                (record["code"], record["exit_date"]),
            ).fetchall()
        except Exception:
            own = []
        if len(own) < need:
            # 市场已到期, 个股数据不足 (断供/停牌/退市) → 留痕
            return DUE_SKIPPED_NO_DATA, 0
        due_date = own[need - 1][0]
        try:
            row = conn.execute(
                "SELECT close FROM daily_klines WHERE code=? AND date=?",
                (record["code"], due_date),
            ).fetchone()
            if row and row[0]:
                return DUE_DONE, float(row[0])
            # 到期日无 K 线 (停牌等): 取到期日前最近收盘 (原语义保留)
            row = conn.execute(
                "SELECT close FROM daily_klines WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
                (record["code"], due_date),
            ).fetchone()
        except Exception:
            row = None
        if row and row[0]:
            return DUE_DONE, float(row[0])
        # 有 N 天日期但取不到任何价 → 同样须留痕
        return DUE_SKIPPED_NO_DATA, 0

    def get_tracking(self, code: str) -> list[dict]:
        self._ensure_table()
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT * FROM post_exit_tracking WHERE code = ? ORDER BY check_point",
            (code,)
        ).fetchall()
        return [dict(r) for r in rows]

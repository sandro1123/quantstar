"""health/readiness.py — 数据完备性检查"""
from common.database import Database
from common.utils import validate_identifier
from data_layer._models import ReadinessReport, TableStatus


def fiscal_window_ok(audit_date: str, actual: str) -> bool:
    """季表披露窗口判 — 卡2 readiness-announce-aware-unify 同源函数。

    直接复用 freshness_audit._fiscal_window_ok (同一真值源, 不造第二套 —
    DL-30 catalog 判例「不造第二套机制」同族)。三头哨兵
    (freshness_audit/readiness/health) 都经本函数判 stock_financials 类季表。

    语义: actual 所在季的下一季披露窗未开 → True (表级滞后=季度语义非断血);
    下一季季末+45 天 (披露集中期止) 已过 → False (真断血窗口)。
    """
    from data_layer.pipelines.freshness_audit import (
        FreshnessAuditPipeline as _FAP,
    )
    return _FAP._fiscal_window_ok(audit_date, actual)


def fiscal_stale_days(audit_date: str, actual: str) -> int | None:
    """卡3: actual 距 audit_date 的天数 (陈数据显形用; 判例 #128 反向面)。

    freshness_audit._days_behind 同源 (不造第二套)。None=actual 空/非法。
    """
    from data_layer.pipelines.freshness_audit import _days_behind
    return _days_behind(audit_date, actual)


class DataReadiness:
    REQUIRED_TABLES = ["daily_klines", "stock_info", "stock_financials", "stock_valuation"]

    # QS 卡 financials-q3-window-guard 件③ (2026-10-02): 季报表宽容窗口。
    # stock_financials 的 date=报告期末日 (季度语义), 对「当日」判恒滞后 91 天
    # (delay=131040min 假警报, 哨兵④ announce 口径才是对的 — 双哨兵打架)。
    # 季表判据 = last_date 为最近季度末 且 today <= 季末 + 披露窗宽 (45 天, 与
    # 哨兵④ announce_off_season_stale_days 同源) → ok; 超窗 → delayed。
    FISCAL_TABLES = {"stock_financials"}
    FISCAL_GRACE_DAYS = 45

    # DL-07: 冷采集表新鲜度检查清单 (表→日期列)。表缺失或当日无行 → not fresh,
    # 但不影响主 readiness (冷采集不进选股链路)。DL-12 将结果落 freshness_audit。
    # #116 面一: +lhb_daily — 龙虎榜缺日 11 个交易日全盲 (不在清单=零行日无痕),
    # 补入后 freshness_audit 每日如实记 missing, 09:30 轻哨 (freshness_sentinel)
    # 读前日审计行 → 飞书告警, 零行日不再静默。
    COLLECT_TABLES = {
        "limit_up_ladder": "snapshot_date",
        "limit_break_pool": "date",
        "hot_stock_history": "date",
        "skyrocket_list": "date",
        "stock_anomaly_reasons": "date",
        "sector_fund_flow": "date",
        "auction_benchmark": "date",
        "auction_snapshot": "date",
        # lhb_daily 已移 T1_COLLECT_TABLES (W2 卡①): 龙虎榜是 T+1 源,
        # 留在此处按当日判会每日假 missing (与 index_daily 同病同治)。
        # 注: adjust_factors 故意不入本清单 —— 它是 18:30 顶层腿而本审计 16:22 跑,
        # 直接入清单会每日假 missing (需时点感知的期望值, 另出提案)。
    }

    # 卡组二刀1 (2026-10-19, 派单 sm-33828e0a16304c5a): 交易日工件类注册表 —
    # 炸板池/异动原因/竞价基准快照只在交易日有数据, 假日审计当日行=0 是
    # 天然形态非断供。audit 逐日史实证: 10-02/05/06/07 假日每天假报这四表
    # missing (alert#127 滚动告警的假报面)。判据走 TradingCalendar (静态
    # holiday 表, 不读 daily_klines — 库停更=病源, 拿它当锚=用有病产物当
    # 基线, 同 prev_trading_day_before v4 注释)。
    # 自然日供数类 (hot_stock_history/skyrocket_list/limit_up_ladder/
    # sector_fund_flow) 不在此册: fuyao 按自然日供数, 10-02 实测 30 行,
    # 假日照判。两册不得交叠 — 同表两语义=判定漂移。
    TRADING_DAY_ARTIFACT_TABLES = {
        "limit_break_pool": "date",
        "stock_anomaly_reasons": "date",
        "auction_benchmark": "date",
        "auction_snapshot": "date",
    }

    # W2 卡① (2026-09-20): T+1 源注册表 —— 这类源的「当日数据」次日才落库
    # (index_daily/lhb_daily 由 akshare_sina 次日 ~08:03 落), freshness_audit
    # 16:22 跑时当日行必然未落, 按当日判 fresh 会恒 missing → 每日假阳
    # (alert#115 同族; 与上方 adjust_factors 「时点感知的期望值」同病, 此处落地)。
    # 判据: 查「上一交易日」而非当日 —— 上一交易日有行=源履约(ok),
    # 上一交易日无行=源该发未发(真 missing), 零历史=missing(哨兵不哑)。
    T1_COLLECT_TABLES = {
        # AUD-014a 原注释: index_daily 是 ddp (15:30) 的条件子管道, 每日应有行,
        # 缺日须留痕 (与 #116 lhb_daily "缺日 11 个交易日全盲" 同族)。
        "index_daily": "date",
        "lhb_daily": "date",
    }

    # P2-c (2026-11-11): 补采轮已知时点注册表 —— 「审计时点早于数据到达时点」
    # 的显式解法 (判例 #11: 判据执行时点 ≠ 数据到达时点)。
    #
    # 病灶实证 (生产 qs.db freshness_audit 逐日史):
    #   10-27 / 10-30 / 11-02 / 11-03 四天各有 **同样这 5 张表** 被判 missing,
    #   而 shortterm_recollect 17:00 前 (16:40) 每日 success 补采。
    #   复核: 用生产同一份代码对同一份库重跑 check_collect_freshness("2026-11-02"),
    #   这 5 张表全部 fresh=True (355/28/30/30/630 行)。⇒ 不是没数据, 也不是
    #   判据写错, 是 **16:22 判了 16:40 才补的东西**。
    #
    # 语义边界 (为什么不是「无脑判 ok」):
    #   - 仍记 missing 会每天假阳 (alert#127 同族), 且 S5/日报跟着红;
    #   - 直接判 ok 会掩盖「补采也失败」的真实断供 — 那是更坏的假绿。
    #   ⇒ 用**独立状态** delayed_by_recollect: 既不当 missing 报红, 也不当 ok
    #     骗人; 由补采轮跑完后的二次核验 (orchestrator 收盘段) 落最终状态。
    #
    # 表名→补采轮所属管道。未列入本册的表保持原判 (当日无行=missing)。
    # 表名→补采轮所属管道 + 该轮的计划时点 (HH:MM)。
    # **只有当前时刻早于补采轮计划时点**才判 pending —— 这保证了:
    #   - 16:22 审计时 (补采未跑) → delayed_by_recollect, 不每天假阳;
    #   - 补采轮跑过后再查 (16:40 之后 / 次日回看) → 仍按 missing 如实显形。
    # 否则「交易日 0 行必须显形」这条硬约束会被 pending 洗掉 (假绿)。
    RECOLLECT_BACKED_TABLES = {
        "limit_up_ladder": "shortterm_recollect",
        "limit_break_pool": "shortterm_recollect",
        "hot_stock_history": "shortterm_recollect",
        "skyrocket_list": "shortterm_recollect",
        "stock_anomaly_reasons": "shortterm_recollect",
    }
    # 补采轮计划时刻 (与 config/defaults/ops.yaml shortterm_recollect.schedule 同源;
    # 生产实际 crontab 为 16:40, 代码默认 16:30 — 取较早者做保守边界)。
    RECOLLECT_WINDOW_START = "16:30"

    def __init__(self, db: Database):
        self.db = db

    def check_collect_freshness(self, date: str) -> dict[str, dict]:
        """DL-07 冷采集表新鲜度: {table: {"fresh": bool, "rows": int}}

        当日源查当日; T+1 源 (T1_COLLECT_TABLES) 查上一交易日 (真实交易日历,
        走 daily_klines MAX(date)<date —— 周一查周五而非周日)。
        """
        conn = self.db.get_connection()
        result: dict[str, dict] = {}
        # 卡组二刀1: 假日守卫 — 非交易日 audit 日, 工件类表当日无行不是断供。
        # 日历判不了的日期 (未来/坏格式/日历库空) 一律不跳过 — 保守判当日,
        # 哨兵不哑 (TDD: test_unknown_calendar_date_not_skipped)。
        holiday = self._is_holiday(date)
        for table, date_col in self.COLLECT_TABLES.items():
            if not validate_identifier(table, "table"):
                continue
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {date_col} = ?", (date,)
                ).fetchone()
                rows = row[0] if row else 0
            except Exception:
                rows = 0  # 表不存在 (管线未首跑) 视为不新鲜
            if rows == 0 and holiday and table in self.TRADING_DAY_ARTIFACT_TABLES:
                # 假日工件表: 不判 missing, 显形 skipped=holiday —
                # 「假日缺」不是「无信息」(铁律#12: 判了没广播同族)。
                result[table] = {"fresh": None, "rows": 0, "skipped": "holiday"}
                continue
            if rows == 0 and table in self.RECOLLECT_BACKED_TABLES and self._recollect_pending(date):
                # P2-c: 本表有后置补采轮 (16:40), 而本审计 16:22 跑 —
                # 此刻无行既不能断言断供也不能断言履约。显式记 pending 态,
                # 由 _write_row 落 status='delayed_by_recollect' (非 missing),
                # 不当日告警; 真断供由补采轮自身成败与次日审计显形。
                #
                # **边界**: 仅当「审计时点早于补采轮计划时点」才 pending。
                # 16:40 之后 (或回看历史日) 再查 → 仍走原 missing 判,
                # 保证「交易日 0 行必须显形」不被洗成假绿。
                result[table] = {"fresh": None, "rows": 0,
                                 "pending_recollect": self.RECOLLECT_BACKED_TABLES[table]}
                continue
            result[table] = {"fresh": rows > 0, "rows": rows}
        prev = self._prev_trading_day(conn, date)
        for table, date_col in self.T1_COLLECT_TABLES.items():
            if not validate_identifier(table, "table"):
                continue
            if prev is None:
                # 无交易日历锚 (daily_klines 空) → 保守判不新鲜, 哨兵不哑
                result[table] = {"fresh": False, "rows": 0}
                continue
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {date_col} = ?", (prev,)
                ).fetchone()
                rows = row[0] if row else 0
            except Exception:
                rows = 0
            result[table] = {"fresh": rows > 0, "rows": rows}
        return result

    @staticmethod
    def _recollect_pending(date: str) -> bool:
        """P2-c: 审计时点是否早于补采轮计划时点 (仅「当日」适用)。

        - 审计的是**历史日**(date < today) → 补采轮早已跑过 → 不 pending,
          真缺数如实判 missing (回看历史/次日复核不被洗成假绿);
        - 审计的是**今日**且当前时刻 < RECOLLECT_WINDOW_START → pending;
        - 审计的是今日且补采窗已过 → 不 pending, 照常判 missing。
        任何异常 → False (保守判 missing, 哨兵不哑)。
        """
        try:
            from datetime import datetime as _dt
            from common.utils import bj_date
            today = bj_date()
            if str(date)[:10] != str(today)[:10]:
                return False  # 历史日: 补采轮已跑完, 不给 pending 豁免
            hhmm = _dt.now().strftime("%H:%M")
            return hhmm < DataReadiness.RECOLLECT_WINDOW_START
        except Exception:
            return False

    @staticmethod
    def _is_holiday(date: str) -> bool:
        """非交易日判定 (日历面, 静态 holiday 表 + 周末)。

        与 prev_trading_day_before 同源 — 不读 daily_klines (库停更=病源,
        拿它当锚=用有病产物当基线)。判不了 (坏格式/异常) → False (不跳过,
        保守判当日, 哨兵不哑)。
        """
        try:
            from common.calendar import TradingCalendar
            return not TradingCalendar().is_trading_day(date)
        except Exception:
            return False

    @staticmethod
    def _prev_trading_day(conn, date: str) -> str | None:
        """上一交易日 = daily_klines 中 < date 的 MAX(date) (真实交易日历)。"""
        try:
            row = conn.execute(
                "SELECT MAX(date) FROM daily_klines WHERE date < ?", (date,)
            ).fetchone()
            return row[0] if row and row[0] else None
        except Exception:
            return None

    def check(self, date: str) -> ReadinessReport:
        conn = self.db.get_connection()
        tables = {}
        all_ready = True
        for table in self.REQUIRED_TABLES:
            if not validate_identifier(table, "table"):
                continue
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM {table}",
            ).fetchone()
            last_date = None
            if table != "stock_info":
                try:
                    date_row = conn.execute(
                        f"SELECT MAX(date) as last_date FROM {table}"
                    ).fetchone()
                    last_date = date_row[0] if date_row else None
                except Exception:
                    last_date = None
            has_data = row[0] > 0 if row else False
            tables[table] = TableStatus(
                table=table,
                has_data=has_data,
                record_count=row[0] if row else 0,
                last_date=last_date,
            )
            if not has_data:
                all_ready = False
            # 件③: 季表 status 由 freshness_audit 侧按窗口判 (见下), 此处
            # readiness 只判 has_data 不变 — 窗口判在 _write_row 侧落
            # freshness_audit 行 (不在这里, 保持 readiness 纯 has_data 语义)。
        return ReadinessReport(date=date, is_ready=all_ready, tables=tables)


def prev_trading_day_before(date_str: str) -> str | None:
    """上一交易日 = 从 date_str 前一日往回找第一个 is_trading_day (日历面)。

    v4 (2026-10-04): health() 假日陈旧判定的「上一交易日」锚 — 不读
    daily_klines (库停更=病源, 拿它当锚=用有病产物当基线)。静态推算
    (TradingCalendar._holidays + weekday), 与 S5 未来日回退语义同源。
    """
    from datetime import datetime as _dt, timedelta as _td
    from common.calendar import TradingCalendar
    try:
        d = _dt.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    cal = TradingCalendar()
    for i in range(1, 31):
        cand = (d - _td(days=i)).strftime("%Y-%m-%d")
        if cal.is_trading_day(cand):
            return cand
    return None

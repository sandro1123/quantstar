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

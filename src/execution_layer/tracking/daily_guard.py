# -*- coding: utf-8 -*-
"""execution_layer/tracking/daily_guard.py — REC 每日一致性守护腿 (通电卡, 2026-09-24)。

统筹接线3: consistency_check() + coverage() 需每日调度腿 (否则「每日自问」无载体)。
挂 retrospect_settlement 同族 (盘后回看推荐语义契合), crontab 由统筹 22:05 窗亲挂。

语义: 扫全部未 closed 的跟踪记录, 对每只跑 consistency_check (理由是否仍成立);
覆盖率落库。**不依赖 paper_trading 持仓** (推过就跟踪, 不问买没买)。
"""
from __future__ import annotations

import sqlite3

from common.logging import get_logger
from common.utils import bj_date

logger = get_logger("execution.rec_tracking_daily")

COVERAGE_DDL = """
CREATE TABLE IF NOT EXISTS recommendation_tracking_coverage (
    date TEXT PRIMARY KEY,
    tracked_total INTEGER NOT NULL DEFAULT 0,
    advised_today INTEGER NOT NULL DEFAULT 0,
    coverage_ratio REAL,
    silent_flip_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""


def run_daily_guard(db, asof: str | None = None) -> dict:
    """每日守护: 逐只跟踪股自问「N 天前推荐它的理由今天还成立吗」+ 覆盖率落库。

    返回 {asof, tracked_total, advised_today, coverage_ratio, silent_flip_count}。

    理由成立性判定 (invalidation): 读模块1a rationale 的 invalidation_conditions
    (条件失效声明), 当日有失效信号 → still_valid=False。当前取保守形态:
    rationale 缺失/无失效条件 → still_valid=True (不臆断失效);
    rationale 有 invalidation_conditions 且当日 K线跌破止损 → False。
    """
    from execution_layer.tracking.recommendation_tracker import RecommendationTracker
    from execution_layer.migrations import ensure_schema

    ensure_schema(db)
    asof = asof or bj_date()
    conn = db.get_connection()
    conn.execute(COVERAGE_DDL)
    conn.commit()

    tracker = RecommendationTracker(db)
    rows = conn.execute(
        "SELECT code FROM recommendation_tracking WHERE state != 'closed'").fetchall()
    codes = [r[0] for r in rows]

    # REC-M4-followup (2026-09-24 统筹补充): 健康面缺失标注 —— 该传没传是
    # 隐患, 「所有腿状态全未传入」≠ 默认高档。观察窗先看 (logger 显式点名,
    # 不动 schema; 需持久留痕再议)。
    health_face_missing: list[str] = []
    silent_flips = 0
    for code in codes:
        face = _leg_health_face(conn, code, asof)
        if not face:
            health_face_missing.append(code)
            logger.warning(
                f"[{code}] 一致性守护: 腿状态健康面全缺 (该传没传), "
                f"按「无法判定」处理不按健康处理 — 请检查接线健康面来源")
        still_valid, reason = _check_still_valid(conn, code, asof)
        rep = tracker.consistency_check(code=code, asof=asof,
                                        still_valid=still_valid,
                                        invalidation_reason=reason)
        if rep["silent_flip"]:
            silent_flips += 1

    cov = tracker.coverage(asof=asof)
    conn.execute(
        "INSERT OR REPLACE INTO recommendation_tracking_coverage "
        "(date, tracked_total, advised_today, coverage_ratio, silent_flip_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (asof, cov["tracked_total"], cov["advised_today"],
         cov["coverage_ratio"], silent_flips))
    conn.commit()

    result = {**cov, "asof": asof, "silent_flip_count": silent_flips,
              "health_face_missing": health_face_missing}
    if health_face_missing:
        logger.warning(f"REC 每日守护: {len(health_face_missing)} 只健康面缺失 "
                       f"(不判降档也不判健康 — 数据面待接线): {health_face_missing}")
    if silent_flips:
        logger.warning(f"REC 每日守护: {silent_flips} 只无声翻脸显形 (需补建议依据)")
    else:
        logger.info(f"REC 每日守护: {cov['tracked_total']} 只跟踪中, "
                    f"覆盖 {cov['advised_today']}, 零无声翻脸")
    return result


def _leg_health_face(conn, code: str, asof: str) -> dict:
    """腿健康面 (llm_debate/kline_valuation/moneyflow/news_intel → ok/missing).

    F-22b (2026-10-22, 统筹裁示 sm-0efa96db424644fa): 接真实数据源.
    此前恒返 {} (无来源) —— 70 只跟踪票每天全部报「健康面缺失」, 23 天如此,
    且被写成「设计性表现、属预期」= 把「没实现」说成「有意为之」(F-22 同族第三例).

    四腿判据 (统筹裁示, 逐条钉死):
      llm_debate      = 该票**其 rec_date 当日**有 deep_debate 记录
                        (跟踪票是历史推荐票, 辩论腿只在它被推荐那天跑过;
                         用 asof 判是范畴错误 —— 今天根本没辩论它。
                         rec_date 当日也无记录 → missing, 是真实缺失)
      kline_valuation = daily_klines 与 stock_valuation **都在**当日才算 ok
                        (单表到一半 → missing, 不得单表充数)
      moneyflow       = stock_moneyflow 当日有该票
      news_intel      = stock_news 当日有该票 (不放宽: 个股新闻覆盖率本就低,
                        52/53 恒 missing 是实话; 若致降档过猛是 R4 权重该调,
                        不是判据该松)

    红线 (铁律#12 诚实水位):
      - 「ok」只由「该腿当日数据真实存在」这一**正面证据**产生;
      - 取不到任何腿状态 (表不可用/查询异常/无跟踪行) → 仍返 {} 且由调用方
        显式报缺失, **不得**静默判健康, **不得**用猜测值冒充 'ok';
      - 查询异常不炸主链 (fail-soft), 但异常时按「无证据」处理 = 缺失。
    """
    # 腿状态取不到任何正面证据时一律按「缺失」, 绝不用默认值冒充健康.
    _MISSING = "missing"
    _OK = "ok"

    def _has(table: str, date_col: str = "date",
             extra_where: str = "", extra_args: tuple = ()) -> bool:
        """该票在 table 当日是否有行 (查不到 = False = 无正面证据)."""
        sql = (f"SELECT 1 FROM {table} WHERE code=? AND {date_col}=?"
               + (f" {extra_where}" if extra_where else "") + " LIMIT 1")
        try:
            row = conn.execute(sql, (code, asof) + extra_args).fetchone()
        except Exception:
            # 表不可用 / 列不存在 / 查询异常 → 无证据, 按缺失处理, 不炸主链.
            return False
        return row is not None

    def _has_on(date_str: str) -> bool:
        """该票在指定日期的 agent_predictions(deep_debate) 是否有行."""
        try:
            row = conn.execute(
                "SELECT 1 FROM agent_predictions WHERE code=? AND date=? "
                "AND agent_name='deep_debate' LIMIT 1", (code, date_str)).fetchone()
        except Exception:
            return False
        return row is not None

    def _rec_date() -> str:
        """该票最近一次推荐的 rec_date (辩论腿的真实发生日)."""
        try:
            row = conn.execute(
                "SELECT rec_date FROM recommendation_tracking "
                "WHERE code=? AND state != 'closed' "
                "ORDER BY rec_date DESC, id DESC LIMIT 1", (code,)).fetchone()
        except Exception:
            return ""
        if not row:
            return ""
        return str(row[0] if not isinstance(row, sqlite3.Row) else row["rec_date"])

    # llm_debate: 按该票 rec_date 当日判 (不是 asof).
    _rd = _rec_date()
    llm_debate = _OK if (_rd and _has_on(_rd)) else _MISSING

    # kline_valuation: 两表都在才算 ok.
    kline_valuation = _OK if (_has("daily_klines") and _has("stock_valuation")) else _MISSING

    moneyflow = _OK if _has("stock_moneyflow") else _MISSING
    news_intel = _OK if _has("stock_news") else _MISSING

    face = {
        "llm_debate": llm_debate,
        "kline_valuation": kline_valuation,
        "moneyflow": moneyflow,
        "news_intel": news_intel,
    }

    # 判据2 (钉2): 「健康面整体不可得」→ 返 {} 交调用方显式报缺失.
    # 严格区分两件事 (二者不可混):
    #   ① 健康面**不可得** (连该票的跟踪行都查不到 / 腿表整体不可用)
    #      → 返 {}; 调用方报「健康面缺失」= 故障态, 不判健康也不判降档.
    #   ② 健康面**可得但四腿都缺** (该票确实当日一条数据都没有)
    #      → 返四腿 missing; 这是**逐腿事实**, 不是故障 —— 必须如实报出来,
    #        否则与①混同 = 把「数据坏了」说成「这票没数据」, 反向造假象.
    # 判①的信号 = 取不到该票的跟踪行 (无 rec_date 可依), 此时连「这票是谁」
    # 都不可得, 谈不上腿状态.
    if not _rd:
        return {}

    return face


def _check_still_valid(conn, code: str, asof: str) -> tuple[bool, str]:
    """理由是否仍成立 (保守: 无证据判成立, 不臆断失效)。

    止损/目标价在**入池侧** (stock_pool), 不在 tracking 表 (只读引用原则:
    tracking 不复制持仓语义)。故止损从 stock_pool 读, 与入池守卫同源
    (模块2 f205a941 的 stop_loss 列)。
    """
    row = conn.execute(
        "SELECT invalidation_conditions FROM recommendation_rationale "
        "WHERE code=? ORDER BY rec_date DESC LIMIT 1", (code,)).fetchone()
    if not row or not row[0]:
        return True, ""
    # rationale 有失效条件: 检查当日是否触发 (跌破止损 = 最硬信号)
    try:
        tr = conn.execute(
            "SELECT stop_loss FROM stock_pool WHERE code=?", (code,)).fetchone()
    except Exception:
        return True, ""  # stock_pool 不可得 → 保守判成立 (不臆断)
    kl = conn.execute(
        "SELECT close FROM daily_klines WHERE code=? AND date<=? "
        "ORDER BY date DESC LIMIT 1", (code, asof)).fetchone()
    if tr and tr[0] and kl and kl[0] and kl[0] < tr[0]:
        return False, f"跌破止损线 {tr[0]} (当日收 {kl[0]})"
    return True, ""

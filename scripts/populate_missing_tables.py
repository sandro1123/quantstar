"""数据管道 — 从 AkShare 填充缺失的行情数据表

用法: PYTHONPATH=src python3 scripts/populate_missing_tables.py
"""
import time
import sys
from common.database import Database
from common.logging import get_logger

logger = get_logger("scripts.populate")

RETRY_LIMIT = 3
RETRY_DELAY = 5


def ak_retry(func, *args, **kwargs):
    for attempt in range(RETRY_LIMIT):
        try:
            import akshare as ak
            return func(ak, *args, **kwargs)
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1}/{RETRY_LIMIT} failed: {e}")
            if attempt < RETRY_LIMIT - 1:
                time.sleep(RETRY_DELAY)
    return None


def populate_lhb_daily(conn, start_date: str, end_date: str):
    """填充龙虎榜数据"""
    result = ak_retry(lambda ak: ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date))
    if result is None or len(result) == 0:
        logger.warning("LHB 数据获取失败")
        return 0
    count = 0
    for _, row in result.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO lhb_daily (code, date, net_buy_amount, total_buy, total_sell) VALUES (?, ?, ?, ?, ?)",
            (row["代码"], row["上榜日"],
             float(row.get("龙虎榜净买额", 0) or 0),
             float(row.get("龙虎榜买入额", 0) or 0),
             float(row.get("龙虎榜卖出额", 0) or 0)),
        )
        count += 1
    conn.commit()
    logger.info(f"LHB: {count} 条")
    return count


def populate_concept_daily(conn, date: str):
    """填充概念板块数据 — 分批处理避免超时"""
    boards = ak_retry(lambda ak: ak.stock_board_concept_name_em())
    if boards is None or len(boards) == 0:
        logger.warning("概念板块获取失败")
        return 0

    total = 0
    for idx, (_, row) in enumerate(boards.iterrows()):
        name = row["板块名称"]
        change_pct = float(row.get("涨跌幅", 0) or 0)
        try:
            cons = ak_retry(lambda ak, s=name: ak.stock_board_concept_cons_em(symbol=s))
            if cons is None:
                continue
            for _, c in cons.iterrows():
                conn.execute(
                    "INSERT OR IGNORE INTO concept_daily (code, date, concept_name, change_pct) VALUES (?, ?, ?, ?)",
                    (c["代码"], date, name, change_pct),
                )
                total += 1
            if idx % 50 == 0 and idx > 0:
                conn.commit()
                logger.info(f"  概念: {idx}/{len(boards)}, {total} 条")
        except Exception:
            continue
    conn.commit()
    logger.info(f"概念: {total} 条")
    return total


def populate_analyst_forecasts(conn):
    """填充分析师预测数据"""
    for name in ["stock_profit_forecast", "stock_forecast_adjust",
                 "stock_analyst_recommend", "stock_rating_forecast"]:
        result = ak_retry(lambda ak, n=name: getattr(ak, n)())
        if result is not None and len(result) > 0:
            count = 0
            for _, row in result.iterrows():
                conn.execute(
                    "INSERT OR IGNORE INTO analyst_forecasts (code, date, consensus_score, target_price, rating) VALUES (?, ?, ?, ?, ?)",
                    (row.get("代码", ""), row.get("日期", ""),
                     float(row.get("consensus_score", 0) or 0),
                     float(row.get("target_price", 0) or 0),
                     str(row.get("rating", "") or "")),
                )
                count += 1
            conn.commit()
            logger.info(f"分析师: {count} 条 (来自 {name})")
            return count
    logger.warning("分析师预测: 无可用接口")
    return 0


def main():
    db = Database("data/qs.db")
    conn = db.get_connection()
    date = "2026-07-17"

    logger.info("=== 开始填充缺失数据 ===")

    # 1. 龙虎榜
    n = conn.execute("SELECT COUNT(*) FROM lhb_daily").fetchone()[0]
    if n == 0:
        populate_lhb_daily(conn, "20260701", "20260717")
    else:
        logger.info(f"LHB 已有 {n} 条，跳过")

    # 2. 概念板块
    n = conn.execute("SELECT COUNT(*) FROM concept_daily").fetchone()[0]
    if n == 0:
        populate_concept_daily(conn, date)
    else:
        logger.info(f"概念已有 {n} 条，跳过")

    # 3. 分析师预测
    n = conn.execute("SELECT COUNT(*) FROM analyst_forecasts").fetchone()[0]
    if n == 0:
        populate_analyst_forecasts(conn)
    else:
        logger.info(f"分析师已有 {n} 条，跳过")

    logger.info("=== 完成 ===")


if __name__ == "__main__":
    main()
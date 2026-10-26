"""从 zzshare 免费数据源填充缺失的 A 股数据表

数据来源: https://github.com/zzquant/zzshare (免费, 无需 API Key)
支持: 龙虎榜、板块/概念成分股、涨停原因、市场情绪

用法: PYTHONPATH=src python3 scripts/populate_from_zzshare.py
"""
from zzshare.client import DataApi
from common.database import Database
from common.logging import get_logger

logger = get_logger("scripts.zzshare")
api = DataApi()


def populate_lhb():
    """填充龙虎榜数据 — 从 zzshare 获取"""
    db = Database("data/qs.db")
    conn = db.get_connection()
    existing = conn.execute("SELECT COUNT(*) FROM lhb_daily").fetchone()[0]
    if existing > 100:
        logger.info(f"LHB 已有 {existing} 条，跳过")
        return

    rows = api.lhb_list(date1="20260701", date2="20260717")
    count = 0
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO lhb_daily (code, date, net_buy_amount, total_buy, total_sell, drama_score) VALUES (?, ?, ?, ?, ?, ?)",
            (r["stock_code"], "2026-07-17",
             float(r.get("buy_in", 0) or 0),
             float(r.get("buy_in", 0) or 0) if float(r.get("buy_in", 0) or 0) > 0 else 0,
             float(r.get("buy_in", 0) or 0) if float(r.get("buy_in", 0) or 0) < 0 else 0,
             0.0),
        )
        count += 1
    conn.commit()
    logger.info(f"LHB: {count} 条")


def populate_concept_from_limitup():
    """从涨停原因数据中提取板块/概念信息"""
    db = Database("data/qs.db")
    conn = db.get_connection()
    existing = conn.execute("SELECT COUNT(*) FROM concept_daily").fetchone()[0]
    if existing > 1000:
        logger.info(f"概念已有 {existing} 条，跳过")
        return

    # 用涨停原因接口获取板块-股票映射
    data = api.review_uplimit_reason(date="20260717")
    if not data:
        logger.warning("涨停原因数据为空")
        return

    count = 0
    for plate in data:
        plate_name = plate.get("plate_name", "")
        plate_code = plate.get("plate_code", "")
        change_pct = float(plate.get("plate_score", 0) or 0) / 100
        for stock in plate.get("stocks", []):
            code = stock.get("stock_code", "")
            if code:
                conn.execute(
                    "INSERT OR IGNORE INTO concept_daily (code, date, concept_name, change_pct) VALUES (?, ?, ?, ?)",
                    (code, "2026-07-17", plate_name, change_pct),
                )
                count += 1
    conn.commit()
    logger.info(f"概念(涨停来源): {count} 条")


def populate_concept_from_stock_basic():
    """从 stock_basic 获取股票行业信息"""
    db = Database("data/qs.db")
    conn = db.get_connection()

    stocks = api.stock_basic()
    if not len(stocks):
        logger.warning("stock_basic 数据为空")
        return

    count = 0
    for _, r in stocks.iterrows():
        code = r.get("code", "")
        industry = r.get("industry", "") or r.get("industry_name", "") or ""
        if industry:
            conn.execute(
                "INSERT OR IGNORE INTO concept_daily (code, date, concept_name, change_pct) VALUES (?, ?, ?, ?)",
                (code, "2026-07-17", industry, 0.0),
            )
            count += 1
    conn.commit()
    logger.info(f"概念(行业来源): {count} 条")


def main():
    logger.info("=== 开始从 zzshare 填充数据 ===")

    populate_lhb()
    populate_concept_from_limitup()
    populate_concept_from_stock_basic()

    db = Database("data/qs.db")
    conn = db.get_connection()
    for table in ["concept_daily", "lhb_daily"]:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        logger.info(f"{table}: {n} 条")
    logger.info("=== 完成 ===")


if __name__ == "__main__":
    main()
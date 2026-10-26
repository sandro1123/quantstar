"""DL-21: stock_info.list_date 回填 — dry-run 默认, apply 才写 (写库三步曲)

现状: list_date 覆盖仅 198/5998 (3.3%), 且 198 行全是占位假值
(20260518 x182 / 20260531 x16, 全北交所码)。ST-03 fail-closed 后
上市日期未知→拒绝, 不回填则选股半瘫。

数据源 (实测, akshare 官方交易所名录为主链无限流, tushare 补隙):
- akshare stock_info_sh_name_code: 沪主板 1699 行 + 科创板 614 行含上市日期
- akshare stock_info_sz_name_code: 深市 2896 行含 A股上市日期
- akshare stock_info_bj_name_code: 北交所 336 行 (920xxx 新码) 含上市日期
- tushare stock_basic(L): 5545 行含 list_date, 覆盖旧北交所码 (43/83/87xxx),
  限流 1次/分钟带重试, 仅作补隙 (setdefault 不覆盖 akshare 已有值)

回填策略: 源有真实日期 且 (现值为空 或 现值为占位日期) 才覆盖;
真实日期绝不误改。占位日期 = 同一日期 >=10 行 (真实上市日不会如此集中)。

用法:
  1. dry-run:  python scripts/backfill_list_date.py --db data/qs.db
  2. 正式执行: python scripts/backfill_list_date.py --db data/qs.db --apply --backup
     (--backup 先落 list_date_backup 表)
"""

from __future__ import annotations

import argparse
import sqlite3
import time

from common.logging import get_logger

logger = get_logger("data.backfill_list_date")

# 同一 list_date 达到该行数视为占位假值 (真实上市日不会如此集中)
PLACEHOLDER_THRESHOLD = 10


def fetch_akshare_list_dates() -> dict[str, str]:
    """akshare 官方交易所名录 → {code: list_date(YYYYMMDD)}。主链, 无限流。

    沪主板 + 科创板 + 深市 + 北交所 (920xxx 新码)。
    """
    import akshare as ak
    result: dict[str, str] = {}

    # 沪: 主板 + 科创板 (两表列名一致: 证券代码/上市日期)
    for sym in (None, "科创板"):
        try:
            df = ak.stock_info_sh_name_code() if sym is None \
                else ak.stock_info_sh_name_code(symbol=sym)
            for _, r in df.iterrows():
                code = str(r.get("证券代码", "") or "")
                ld = str(r.get("上市日期", "") or "")
                if code and ld:
                    result[code] = ld.replace("-", "")[:8]
        except Exception as e:
            logger.warning(f"akshare SH({sym}) failed: {str(e)[:100]}")

    # 深: A股代码/A股上市日期
    try:
        df = ak.stock_info_sz_name_code()
        for _, r in df.iterrows():
            code = str(r.get("A股代码", "") or "")
            ld = str(r.get("A股上市日期", "") or "")
            if code and ld:
                result[code] = ld.replace("-", "")[:8]
    except Exception as e:
        logger.warning(f"akshare SZ failed: {str(e)[:100]}")

    # 北: 证券代码/上市日期 (920xxx 新码)
    try:
        df = ak.stock_info_bj_name_code()
        for _, r in df.iterrows():
            code = str(r.get("证券代码", "") or "")
            ld = str(r.get("上市日期", "") or "")
            if code and ld:
                result[code] = ld.replace("-", "")[:8]
    except Exception as e:
        logger.warning(f"akshare BJ failed: {str(e)[:100]}")

    return result


def fetch_tushare_list_dates() -> dict[str, str]:
    """tushare stock_basic(L) → {code: list_date(YYYYMMDD)}, 限流重试。补隙用。"""
    import os

    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN", "")
    if token:
        ts.set_token(token)
    pro = ts.pro_api()
    df = None
    for attempt in range(4):
        try:
            df = pro.stock_basic(exchange="", list_status="L")
            break
        except Exception as e:
            logger.warning(f"tushare stock_basic attempt {attempt + 1}/4 failed: {str(e)[:120]}")
            if attempt < 3:
                time.sleep(62)
    if df is None or df.empty:
        raise RuntimeError("tushare stock_basic(L) 拉取失败")
    result = {}
    for _, r in df.iterrows():
        code = str(r.get("symbol", "") or "")
        ld = str(r.get("list_date", "") or "")
        if code and ld:
            result[code] = ld.replace("-", "")
    return result


def detect_placeholder_dates(conn) -> set[str]:
    """同一日期 >= PLACEHOLDER_THRESHOLD 行 → 占位假值集合。"""
    rows = conn.execute(
        "SELECT list_date, COUNT(*) n FROM stock_info "
        "WHERE list_date != '' GROUP BY list_date HAVING n >= ?",
        (PLACEHOLDER_THRESHOLD,),
    ).fetchall()
    return {r["list_date"] for r in rows}


def plan_backfill(conn, sources: dict[str, str]) -> list[dict]:
    """生成回填计划 [{code, old, new, reason}], reason=fill_empty|overwrite_placeholder。"""
    placeholders = detect_placeholder_dates(conn)
    rows = conn.execute("SELECT code, list_date FROM stock_info").fetchall()
    plan = []
    for r in rows:
        code, old = r["code"], r["list_date"] or ""
        new = sources.get(code, "")
        if not new:
            continue
        if old == "":
            plan.append({"code": code, "old": old, "new": new, "reason": "fill_empty"})
        elif old in placeholders and old != new:
            plan.append({"code": code, "old": old, "new": new, "reason": "overwrite_placeholder"})
    return plan


def backfill(db_path: str, sources: dict[str, str],
             apply: bool = False, backup: bool = False) -> dict:
    """执行回填。apply=False 仅统计; backup=True 先落备份表。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) FROM stock_info").fetchone()[0]
    plan = plan_backfill(conn, sources)
    fill_empty = sum(1 for p in plan if p["reason"] == "fill_empty")
    overwrite = sum(1 for p in plan if p["reason"] == "overwrite_placeholder")
    all_codes = {r["code"] for r in conn.execute("SELECT code FROM stock_info")}
    matched = len(all_codes & set(sources))
    stats = {
        "total_rows": total,
        "source_codes": len(sources),
        "fill_empty": fill_empty,
        "overwrite_placeholder": overwrite,
        "skipped_no_source": total - matched,
        "applied": 0,
    }
    if not apply:
        logger.info(
            f"[dry-run] 共 {total} 行, 源覆盖 {matched}, "
            f"填空 {fill_empty}, 覆盖占位 {overwrite}")
        conn.close()
        return stats

    if backup:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS list_date_backup AS "
            "SELECT code, list_date FROM stock_info")
        conn.commit()
        logger.info("backup table list_date_backup created")

    for p in plan:
        conn.execute(
            "UPDATE stock_info SET list_date = ? WHERE code = ?",
            (p["new"], p["code"]),
        )
        stats["applied"] += 1
    conn.commit()

    after = conn.execute(
        "SELECT COUNT(*) FROM stock_info WHERE list_date != ''").fetchone()[0]
    stats["coverage_after"] = round(after / total * 100, 2) if total else 0.0
    logger.info(f"applied {stats['applied']} list_date backfills, "
                f"coverage {stats['coverage_after']}%")
    conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="DL-21 stock_info.list_date 回填")
    parser.add_argument("--db", required=True, help="qs.db 路径")
    parser.add_argument("--apply", action="store_true",
                        help="实际执行 UPDATE (默认 dry-run)")
    parser.add_argument("--backup", action="store_true",
                        help="apply 前先落 list_date_backup 表")
    args = parser.parse_args()

    sources: dict[str, str] = {}
    try:
        sources.update(fetch_akshare_list_dates())
        logger.info(f"akshare 官方名录: {len(sources)} codes with list_date")
    except Exception as e:
        logger.error(f"akshare 链失败: {str(e)[:150]}")
    # tushare 补隙 (旧北交所码 43/83/87xxx 等 akshare 新表未覆盖的),
    # setdefault 不覆盖 akshare 已有值
    try:
        ts_map = fetch_tushare_list_dates()
        gap = 0
        for code, ld in ts_map.items():
            if code not in sources:
                sources[code] = ld
                gap += 1
        logger.info(f"tushare 补隙: +{gap} codes (总 {len(ts_map)})")
    except Exception as e:
        logger.warning(f"tushare 补隙跳过: {str(e)[:150]}")

    if not sources:
        logger.error("所有源均失败, 终止")
        raise SystemExit(1)

    stats = backfill(args.db, sources, apply=args.apply, backup=args.backup)
    print(stats)


if __name__ == "__main__":
    main()

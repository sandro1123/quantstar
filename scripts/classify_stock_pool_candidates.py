#!/usr/bin/env python3
"""Classify 239 pollution-drill candidates into actionable categories.

Reads drill.json findings, queries stock_info + kline bounds from the
market DB, and produces per-code suggestions with forward/rollback SQL.

Usage:
    python scripts/classify_stock_pool_candidates.py \\
        --market-db ~/qs/data/qs.db \\
        --drill-json ~/qs-repair-backups/phase2_pollution_drill_20260731/drill.json \\
        --drill-db ~/qs-repair-backups/phase2_pollution_drill_20260731/qs.pollution-drill.db \\
        --output-dir /tmp/classify-output
"""

import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

CATEGORIES = [
    "KEEP_ACTIVE",
    "KEEP_HISTORICAL_INACTIVE",
    "CORRECT_MARKET_LABEL",
    "MAP_TO_SUCCESSOR_KEEP_HISTORY",
    "QUARANTINE_TEST_RECORD",
    "NO_CHANGE_NEEDS_REVIEW",
]

SUCCESSOR_MAP = {
    "000024": {"successor": "001979", "note": "招商地产 → 招商蛇口 (2015重组)"},
    "000418": {"successor": "000333", "note": "小天鹅A → 美的集团 (2019吸收合并)"},
    "000527": {"successor": "000333", "note": "美的电器 → 美的集团 (2013换股)"},
    "000522": {"successor": "600332", "note": "白云山A → 白云山 (2013换股)"},
    "000515": {"successor": "000629", "note": "攀渝钛业 → 钒钛股份 (2008重组)"},
    "000406": {"successor": None, "note": "石油大明 — 2006退市, 无后继代码"},
    "000508": {"successor": None, "note": "琼民源A — 1999退市, 无后继代码"},
    "000005": {"successor": None, "note": "ST星源 — 2024退市, 三板代码400xxx不在本系统"},
    "000013": {"successor": None, "note": "*ST石化A — 2004退市, 无后继代码"},
    "000015": {"successor": None, "note": "PT中浩A — 2002退市, 无后继代码"},
    "000023": {"successor": None, "note": "*ST深天 — 2024退市, 三板代码400xxx不在本系统"},
    "000040": {"successor": None, "note": "*ST旭蓝 — 2024退市, 三板代码400xxx不在本系统"},
    "000046": {"successor": None, "note": "*ST泛海 — 2023退市, 三板代码400xxx不在本系统"},
    "000047": {"successor": None, "note": "ST中侨 — 2003退市, 无后继代码"},
    "000150": {"successor": None, "note": "*ST宜康 — 2023退市, 三板代码400xxx不在本系统"},
    "000405": {"successor": None, "note": "ST鑫光 — 2004退市, 无后继代码"},
    "000412": {"successor": None, "note": "ST五环 — 2003退市, 无后继代码"},
    "000413": {"successor": None, "note": "ST旭电 — 2024退市, 三板代码400xxx不在本系统"},
    "000416": {"successor": None, "note": "*ST民控 — 2024退市, 三板代码400xxx不在本系统"},
    "000535": {"successor": None, "note": "*ST猴王 — 2005退市, 无后继代码"},
}


def setup_logging(output_dir: str) -> logging.Logger:
    os.makedirs(output_dir, exist_ok=True)
    logger = logging.getLogger("classify")
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(os.path.join(output_dir, "run.log"), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def open_db_ro(path: str, logger: logging.Logger) -> sqlite3.Connection:
    if not os.path.exists(path):
        logger.error("Database not found: %s", path)
        sys.exit(1)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    verify = conn.execute("PRAGMA query_only").fetchone()
    if not verify or verify["query_only"] != 1:
        logger.error("DB read-only verification failed for %s", path)
        sys.exit(1)
    logger.info("Opened DB (ro): %s", path)
    return conn


def load_candidates(drill_json_path: str, drill_db_path: str, logger: logging.Logger) -> list[dict]:
    findings = []

    with open(drill_json_path, "r", encoding="utf-8") as f:
        drill_data = json.load(f)
    logger.info("Loaded drill.json: %d unique candidates, %s",
                drill_data.get("candidate_unique", 0),
                drill_data.get("findings_counts", {}))

    sample = drill_data.get("sample_findings", {})
    for category, records in sample.items():
        for rec in records:
            findings.append({
                "code": rec[0],
                "name": rec[1],
                "market": rec[2],
                "list_date": rec[3] if len(rec) > 3 else "",
                "is_active": rec[4] if len(rec) > 4 else 1,
                "drill_reason": category,
            })

    qconn = open_db_ro(drill_db_path, logger)
    try:
        qrows = qconn.execute(
            "SELECT code, name, market, list_date, is_active, quarantine_reason "
            "FROM stock_info_quarantine_20260731 ORDER BY code"
        ).fetchall()
        logger.info("Quarantine table has %d rows", len(qrows))

        seen_codes = {f["code"] for f in findings}
        for r in qrows:
            if r["code"] not in seen_codes:
                findings.append({
                    "code": r["code"],
                    "name": r["name"],
                    "market": r["market"],
                    "list_date": r["list_date"] or "",
                    "is_active": r["is_active"],
                    "drill_reason": r["quarantine_reason"],
                })
                seen_codes.add(r["code"])
    finally:
        qconn.close()

    logger.info("Total candidates loaded: %d (from drill.json + quarantine table)", len(findings))
    return findings


def get_stock_info(conn: sqlite3.Connection, code: str) -> dict | None:
    row = conn.execute("SELECT code, name, industry, market, list_date, is_active FROM stock_info WHERE code = ?",
                       (code,)).fetchone()
    if row:
        return dict(row)
    return None


def get_kline_bounds(conn: sqlite3.Connection, code: str) -> dict | None:
    row = conn.execute(
        "SELECT MIN(date) AS first_date, MAX(date) AS last_date, COUNT(*) AS kline_count FROM daily_klines WHERE code = ?",
        (code,),
    ).fetchone()
    if row and row["kline_count"] > 0:
        return dict(row)
    return None


def classify_code(code: str, info: dict | None, kline: dict | None, logger: logging.Logger) -> dict:
    result = {
        "code": code,
        "suggestion": "NO_CHANGE_NEEDS_REVIEW",
        "confidence": "low",
        "rationale": "",
        "sql_generated": False,
    }

    if code == "XXX" and info and info["name"] == "TEST":
        result["suggestion"] = "QUARANTINE_TEST_RECORD"
        result["confidence"] = "high"
        result["rationale"] = "Test record XXX/TEST — no real stock, no klines."
        result["sql_generated"] = True
        return result

    if code == "920808":
        result["suggestion"] = "CORRECT_MARKET_LABEL"
        result["confidence"] = "high"
        bj_name = info["name"] if info else "曙光数创"
        result["rationale"] = f"920808 ({bj_name}) market should be '北交所', not 'SZ'. All other 920xxx codes are '北交所'."
        result["sql_generated"] = True
        return result

    if info is None:
        result["suggestion"] = "NO_CHANGE_NEEDS_REVIEW"
        result["confidence"] = "low"
        result["rationale"] = f"Code {code} not found in stock_info table."
        result["sql_generated"] = False
        return result

    is_placeholder = info["list_date"] == "20260518"

    if is_placeholder:
        successor_info = SUCCESSOR_MAP.get(code, None)
        has_klines = kline is not None

        if successor_info and successor_info["successor"]:
            result["suggestion"] = "MAP_TO_SUCCESSOR_KEEP_HISTORY"
            result["confidence"] = "high"
            kline_desc = (
                f"Keep historical klines ({kline['kline_count']} rows, "
                f"{kline['first_date']} ~ {kline['last_date']}), "
                if has_klines else "No klines on record. "
            )
            result["rationale"] = (
                f"{code} ({info['name']}) — {successor_info['note']}. "
                f"{kline_desc}mark is_active=0."
            )
            result["sql_generated"] = True
            result["successor"] = successor_info["successor"]
            result["kline_count"] = kline["kline_count"] if has_klines else 0
            result["kline_first"] = kline["first_date"] if has_klines else ""
            result["kline_last"] = kline["last_date"] if has_klines else ""
            return result

        if successor_info and successor_info["successor"] is None:
            result["suggestion"] = "KEEP_HISTORICAL_INACTIVE"
            result["confidence"] = "high"
            kline_desc = (
                f"Keep historical klines ({kline['kline_count']} rows, "
                f"{kline['first_date']} ~ {kline['last_date']}), "
                if has_klines else "No klines on record. "
            )
            result["rationale"] = (
                f"{code} ({info['name']}) — {successor_info['note']}. "
                f"No successor in this system. "
                f"{kline_desc}mark is_active=0."
            )
            result["sql_generated"] = True
            result["kline_count"] = kline["kline_count"] if has_klines else 0
            result["kline_first"] = kline["first_date"] if has_klines else ""
            result["kline_last"] = kline["last_date"] if has_klines else ""
            return result

        if has_klines and kline["kline_count"] > 0:
            result["suggestion"] = "KEEP_HISTORICAL_INACTIVE"
            result["confidence"] = "medium"
            result["rationale"] = (
                f"{code} ({info['name']}) — placeholder list_date=20260518, "
                f"has {kline['kline_count']} klines ({kline['first_date']} ~ {kline['last_date']}). "
                f"Likely delisted/historical stock. No known successor mapped. "
                f"Mark is_active=0, keep historical data."
            )
            result["sql_generated"] = False
            result["kline_count"] = kline["kline_count"]
            result["kline_first"] = kline["first_date"]
            result["kline_last"] = kline["last_date"]
            return result

        result["suggestion"] = "NO_CHANGE_NEEDS_REVIEW"
        result["confidence"] = "low"
        result["rationale"] = (
            f"{code} ({info['name']}) — placeholder list_date=20260518, "
            f"no klines. Unclear status."
        )
        result["sql_generated"] = False
        return result

    if kline is None or kline["kline_count"] == 0:
        if info["is_active"] == 1:
            result["suggestion"] = "NO_CHANGE_NEEDS_REVIEW"
            result["confidence"] = "low"
            result["rationale"] = (
                f"{code} ({info['name']}) — active but has no klines. "
                f"May be a new listing or data gap."
            )
            result["sql_generated"] = False
            return result

    result["suggestion"] = "KEEP_ACTIVE"
    result["confidence"] = "high"
    kline_str = ""
    if kline:
        kline_str = f" ({kline['kline_count']} klines, {kline['first_date']} ~ {kline['last_date']})"
    result["rationale"] = (
        f"{code} ({info['name']}) — valid active stock{kline_str}. "
        f"No issues detected."
    )
    result["sql_generated"] = False
    return result


def generate_sql(classified: list[dict], logger: logging.Logger) -> tuple[str, str]:
    forward_lines = [
        "-- DO NOT EXECUTE - NOVA REVIEW REQUIRED",
        "-- ============================================================",
        f"-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"-- Source: phase2_pollution_drill_20260731",
        f"-- Candidates: {len(classified)}",
        "-- ============================================================",
        "--",
        "-- RULES:",
        "--   * No DELETE statements",
        "--   * Every UPDATE has precise WHERE with current-value guard",
        "--   * Forward / Rollback pairs are 1:1",
        "--   * Low/medium confidence items generate NO SQL",
        "--",
    ]
    rollback_lines = [
        "-- DO NOT EXECUTE - NOVA REVIEW REQUIRED (ROLLBACK)",
        "-- ============================================================",
        f"-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"-- Paired 1:1 with proposed-forward.sql",
        "-- ============================================================",
        "--",
        "-- Rollback reverses every forward change in order.",
        "-- Each rollback has the same before-value guard.",
        "--",
    ]

    forward_seq = 0
    for item in classified:
        if not item.get("sql_generated"):
            continue

        code = item["code"]
        suggestion = item["suggestion"]
        rationale = item["rationale"].replace("\n", " ")

        forward_seq += 1
        f_comment = f"-- [{forward_seq}] {code}: {suggestion}"
        r_comment = f"-- [{forward_seq}] {code}: ROLLBACK of {suggestion}"

        if suggestion == "KEEP_HISTORICAL_INACTIVE":
            forward_lines.append("")
            forward_lines.append(f_comment)
            forward_lines.append(f"-- {rationale}")
            forward_lines.append(f"UPDATE stock_info")
            forward_lines.append(f"   SET is_active = 0")
            forward_lines.append(f" WHERE code = '{code}'")
            forward_lines.append(f"   AND is_active = 1;  -- guard: was active")

            rollback_lines.append("")
            rollback_lines.append(r_comment)
            rollback_lines.append(f"UPDATE stock_info")
            rollback_lines.append(f"   SET is_active = 1")
            rollback_lines.append(f" WHERE code = '{code}'")
            rollback_lines.append(f"   AND is_active = 0;  -- guard: was inactive")

        elif suggestion == "CORRECT_MARKET_LABEL":
            forward_lines.append("")
            forward_lines.append(f_comment)
            forward_lines.append(f"-- {rationale}")
            forward_lines.append(f"UPDATE stock_info")
            forward_lines.append(f"   SET market = '北交所'")
            forward_lines.append(f" WHERE code = '{code}'")
            forward_lines.append(f"   AND market = 'SZ';  -- guard: was SZ")

            rollback_lines.append("")
            rollback_lines.append(r_comment)
            rollback_lines.append(f"UPDATE stock_info")
            rollback_lines.append(f"   SET market = 'SZ'")
            rollback_lines.append(f" WHERE code = '{code}'")
            rollback_lines.append(f"   AND market = '北交所';  -- guard: was 北交所")

        elif suggestion == "MAP_TO_SUCCESSOR_KEEP_HISTORY":
            successor = item.get("successor", "")
            forward_lines.append("")
            forward_lines.append(f_comment)
            forward_lines.append(f"-- {rationale}")
            forward_lines.append(f"UPDATE stock_info")
            forward_lines.append(f"   SET is_active = 0")
            forward_lines.append(f" WHERE code = '{code}'")
            forward_lines.append(f"   AND is_active = 1;  -- guard: was active")
            if successor:
                forward_lines.append(f"-- NOTE: Successor {successor} is the active ticker.")

            rollback_lines.append("")
            rollback_lines.append(r_comment)
            rollback_lines.append(f"UPDATE stock_info")
            rollback_lines.append(f"   SET is_active = 1")
            rollback_lines.append(f" WHERE code = '{code}'")
            rollback_lines.append(f"   AND is_active = 0;  -- guard: was inactive")

        elif suggestion == "QUARANTINE_TEST_RECORD":
            forward_lines.append("")
            forward_lines.append(f_comment)
            forward_lines.append(f"-- {rationale}")
            forward_lines.append(f"UPDATE stock_info")
            forward_lines.append(f"   SET is_active = 0")
            forward_lines.append(f" WHERE code = '{code}'")
            forward_lines.append(f"   AND is_active = 1;  -- guard: was active")

            rollback_lines.append("")
            rollback_lines.append(r_comment)
            rollback_lines.append(f"UPDATE stock_info")
            rollback_lines.append(f"   SET is_active = 1")
            rollback_lines.append(f" WHERE code = '{code}'")
            rollback_lines.append(f"   AND is_active = 0;  -- guard: was inactive")

    forward_lines.append("")
    forward_lines.append("-- End of proposed-forward.sql")
    rollback_lines.append("")
    rollback_lines.append("-- End of proposed-rollback.sql")

    return "\n".join(forward_lines), "\n".join(rollback_lines)


def write_outputs(classified: list[dict], output_dir: str, forward_sql: str, rollback_sql: str, logger: logging.Logger):
    json_path = os.path.join(output_dir, "classified_candidates.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(classified, f, indent=2, ensure_ascii=False)
    logger.info("Wrote: %s", json_path)

    csv_path = os.path.join(output_dir, "classified_candidates.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "code", "name", "market", "list_date", "is_active",
            "drill_reason", "suggestion", "confidence", "rationale",
            "kline_count", "kline_first", "kline_last", "successor",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(classified)
    logger.info("Wrote: %s", csv_path)

    forward_path = os.path.join(output_dir, "proposed-forward.sql")
    with open(forward_path, "w", encoding="utf-8") as f:
        f.write(forward_sql)
    logger.info("Wrote: %s", forward_path)

    rollback_path = os.path.join(output_dir, "proposed-rollback.sql")
    with open(rollback_path, "w", encoding="utf-8") as f:
        f.write(rollback_sql)
    logger.info("Wrote: %s", rollback_path)

    write_summary(classified, output_dir, logger)
    write_historical_impact(classified, output_dir, logger)


def write_summary(classified: list[dict], output_dir: str, logger: logging.Logger):
    counter = Counter(item["suggestion"] for item in classified)
    conf_counter = Counter(item["confidence"] for item in classified)
    sql_count = sum(1 for item in classified if item.get("sql_generated"))

    summary_path = os.path.join(output_dir, "category-summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("CATEGORY SUMMARY\n")
        f.write(f"{'=' * 60}\n")
        f.write(f"Total candidates: {len(classified)}\n")
        f.write(f"SQL generated for: {sql_count}\n\n")
        f.write(f"{'Category':<40} {'Count':>6}\n")
        f.write(f"{'-' * 46}\n")
        for cat in CATEGORIES:
            count = counter.get(cat, 0)
            f.write(f"{cat:<40} {count:>6}\n")
        f.write(f"\n{'Confidence':<40} {'Count':>6}\n")
        f.write(f"{'-' * 46}\n")
        for conf, count in sorted(conf_counter.items()):
            f.write(f"{conf:<40} {count:>6}\n")
        f.write(f"\n{'=' * 60}\n")
        f.write("SPECIAL CASES\n")
        f.write(f"{'=' * 60}\n")
        f.write("  1. code '920808' (曙光数创): market label is 'SZ' but all other\n")
        f.write("     920xxx codes are '北交所'. Recommendation: CORRECT_MARKET_LABEL.\n")
        f.write("  2. codes containing 'XXX' or 'TEST': test/pollution records with no\n")
        f.write("     real stock and no klines. Recommendation: QUARANTINE_TEST_RECORD\n")
        f.write("     (mark is_active=0, no data affected).\n")
        placeholder_count = sum(1 for i in classified if i.get("list_date") == "20260518")
        f.write(f"  3. list_date='20260518': placeholder list date shared by "
                f"{placeholder_count} delisted/historical codes in this run. These are\n")
        f.write("     KEEP_HISTORICAL_INACTIVE (mark is_active=0, keep klines) or\n")
        f.write("     MAP_TO_SUCCESSOR_KEEP_HISTORY when a successor is known.\n\n")
        f.write("BREAKDOWN BY CODE\n")
        f.write(f"{'=' * 60}\n")
        for item in sorted(classified, key=lambda x: x["code"]):
            f.write(f"{item['code']:<8} {item['suggestion']:<35} [{item['confidence']:>6}] {item['rationale'][:80]}\n")
    logger.info("Wrote: %s", summary_path)


def write_historical_impact(classified: list[dict], output_dir: str, logger: logging.Logger):
    impact_path = os.path.join(output_dir, "historical-impact.md")
    inactive_items = [i for i in classified if i["suggestion"] in (
        "KEEP_HISTORICAL_INACTIVE", "MAP_TO_SUCCESSOR_KEEP_HISTORY", "QUARANTINE_TEST_RECORD")]
    market_fix_items = [i for i in classified if i["suggestion"] == "CORRECT_MARKET_LABEL"]
    review_items = [i for i in classified if i["suggestion"] == "NO_CHANGE_NEEDS_REVIEW"]

    total_klines_preserved = sum(i.get("kline_count", 0) for i in inactive_items)

    with open(impact_path, "w", encoding="utf-8") as f:
        f.write("# Historical Data Impact Assessment\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("## Summary\n\n")
        f.write(f"- **Total candidates**: {len(classified)}\n")
        f.write(f"- **Codes to mark inactive**: {len(inactive_items)}\n")
        f.write(f"- **Market label corrections**: {len(market_fix_items)}\n")
        f.write(f"- **Needs review**: {len(review_items)}\n")
        f.write(f"- **Klines preserved**: {total_klines_preserved}\n\n")

        f.write("## No DELETE Operations\n\n")
        f.write("This proposal contains **zero DELETE statements**. All changes are:\n")
        f.write("- `UPDATE stock_info SET is_active = 0` — mark historical codes as inactive\n")
        f.write("- `UPDATE stock_info SET market = '北交所'` — fix market label for 920808\n")
        f.write("- All historical kline data remains untouched\n\n")

        f.write("## Successor Mappings\n\n")
        f.write("| Old Code | Old Name | Successor | Notes |\n")
        f.write("|----------|----------|-----------|-------|\n")
        for item in inactive_items:
            if item.get("successor"):
                f.write(f"| {item['code']} | {item.get('name', '')} | {item['successor']} | {item.get('rationale', '')[:60]} |\n")
        f.write("\n")

        if review_items:
            f.write("## Items Requiring Human Review\n\n")
            f.write("| Code | Name | Reason |\n")
            f.write("|------|------|--------|\n")
            for item in review_items:
                f.write(f"| {item['code']} | {item.get('name', '')} | {item.get('rationale', '')[:80]} |\n")
            f.write("\n")

        f.write("## Data Integrity\n\n")
        f.write("- All `daily_klines` rows: **preserved**\n")
        f.write("- All `stock_info` rows: **preserved** (no deletions)\n")
        f.write("- `factor_daily`, `alpha_factors`, `model_predictions`: **preserved**\n")
        f.write("- `stock_pool`: **not affected** by these changes\n")
        f.write("- Rollback is exact 1:1 reverse of forward\n\n")

        f.write("## Risk Assessment\n\n")
        f.write("| Risk | Level | Mitigation |\n")
        f.write("|------|-------|------------|\n")
        f.write("| Historical data loss | None | No DELETE operations |\n")
        f.write("| Query impact | Low | is_active=0 filters out delisted stocks by default |\n")
        f.write("| Rollback safety | High | Every UPDATE has before-value guard |\n")
        f.write("| 920808 market change | Low | SZ→北交所 aligns with all other 920xxx codes |\n")
        f.write("| XXX/TEST quarantine | Low | No real data affected |\n")

    logger.info("Wrote: %s", impact_path)


def main():
    parser = argparse.ArgumentParser(
        description="Classify 239 pollution-drill candidates into actionable categories")
    parser.add_argument("--market-db", required=True, help="Path to qs.db (market data)")
    parser.add_argument("--drill-json", required=True, help="Path to drill.json (findings)")
    parser.add_argument("--drill-db", required=True, help="Path to qs.pollution-drill.db")
    parser.add_argument("--output-dir", default="/tmp/classify-output", help="Output directory")
    args = parser.parse_args()

    output_dir = args.output_dir
    logger = setup_logging(output_dir)

    logger.info("=== classify_stock_pool_candidates.py start ===")
    logger.info("Args: --market-db=%s --drill-json=%s --drill-db=%s --output-dir=%s",
                args.market_db, args.drill_json, args.drill_db, output_dir)

    findings = load_candidates(args.drill_json, args.drill_db, logger)

    conn = open_db_ro(args.market_db, logger)

    classified = []
    for f in findings:
        code = f["code"]
        info = get_stock_info(conn, code)
        kline = get_kline_bounds(conn, code)
        result = classify_code(code, info, kline, logger)

        result["name"] = info["name"] if info else f.get("name", "")
        result["market"] = info["market"] if info else f.get("market", "")
        result["list_date"] = info["list_date"] if info else f.get("list_date", "")
        result["is_active"] = info["is_active"] if info else f.get("is_active", 1)
        result["drill_reason"] = f.get("drill_reason", "")
        if kline:
            result["kline_count"] = kline["kline_count"]
            result["kline_first"] = kline["first_date"]
            result["kline_last"] = kline["last_date"]
        else:
            result["kline_count"] = 0
            result["kline_first"] = ""
            result["kline_last"] = ""

        classified.append(result)
        logger.debug("  %s → %s [%s] %s", code, result["suggestion"], result["confidence"],
                     result["rationale"][:60])

    conn.close()

    forward_sql, rollback_sql = generate_sql(classified, logger)
    write_outputs(classified, output_dir, forward_sql, rollback_sql, logger)

    counter = Counter(item["suggestion"] for item in classified)
    sql_count = sum(1 for item in classified if item.get("sql_generated"))
    logger.info("=== Classification complete ===")
    logger.info("Total: %d | SQL: %d | Categories: %s", len(classified), sql_count, dict(counter))
    logger.info("Output: %s", output_dir)


if __name__ == "__main__":
    main()
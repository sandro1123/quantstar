#!/usr/bin/env python3
"""S2-F2.4 stock_financials 日期格式回填脚本

将 compact 格式日期(20260807)归一化为 ISO(2026-08-07)。
用法:
  1. 备份: sqlite3 data/qs.db ".backup data/qs.db.bak_financials"
     或 cp data/qs.db data/backups/qs_pre_financials_datefmt.db
  2. 隔离验证: python scripts/backfill_financials_datefmt.py --db <隔离库路径> --dry-run
  3. 正式执行: python scripts/backfill_financials_datefmt.py --db data/qs.db
证据: 追加 logs/repair_evidence_s2.jsonl
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

TABLE = "stock_financials"
COMPACT_PATTERN = "????????"
ISO_PATTERN = "????-??-??"


def _to_iso(s: str) -> str:
    compact = s.replace("-", "")
    if len(compact) == 8 and compact.isdigit():
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="stock_financials 日期格式回填")
    ap.add_argument("--db", required=True, help="数据库路径")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    ap.add_argument("--evidence", default="logs/repair_evidence_s2.jsonl")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    compact_rows = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE date GLOB ? AND date NOT GLOB ?",
        (COMPACT_PATTERN, ISO_PATTERN),
    ).fetchone()[0]
    other_rows = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE date NOT GLOB ? AND date NOT GLOB ?",
        (COMPACT_PATTERN, ISO_PATTERN),
    ).fetchone()[0]

    print(f"[{args.db}] {TABLE}: 共 {total} 行, compact 需回填 {compact_rows} 行, 非标准格式 {other_rows} 行")

    if other_rows > 0:
        samples = conn.execute(
            f"SELECT code, date FROM {TABLE} WHERE date NOT GLOB ? AND date NOT GLOB ? LIMIT 5",
            (COMPACT_PATTERN, ISO_PATTERN),
        ).fetchall()
        print(f"  非标准样例: {[(r['code'], r['date']) for r in samples]}")

    if args.dry_run:
        print("[dry-run] 不写入")
        return 0

    if compact_rows == 0:
        print("无需回填, 已是全 ISO")
        return 0

    rows = conn.execute(
        f"SELECT code, date FROM {TABLE} WHERE date GLOB ? AND date NOT GLOB ?",
        (COMPACT_PATTERN, ISO_PATTERN),
    ).fetchall()
    updated = 0
    for r in rows:
        iso = _to_iso(r["date"])
        cur = conn.execute(
            f"UPDATE {TABLE} SET date = ? WHERE code = ? AND date = ?",
            (iso, r["code"], r["date"]),
        )
        updated += cur.rowcount
    conn.commit()

    evidence = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "sprint": "S2",
        "task": "F2.4_financials_datefmt_backfill",
        "db": args.db,
        "rows_updated": updated,
        "before_compact": compact_rows,
        "after_compact": conn.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE date GLOB ? AND date NOT GLOB ?",
            (COMPACT_PATTERN, ISO_PATTERN),
        ).fetchone()[0],
    }
    with open(args.evidence, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    print(f"回填完成: {updated} 行; 证据 {args.evidence}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

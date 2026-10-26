#!/usr/bin/env python3
"""S2-F2.3 daily_klines adjust_type 列添加脚本

O(1) ALTER TABLE, 默认 'qfq'。
用法:
  1. 备份正式库: cp data/qs.db data/backups/qs_pre_adjust_type.db
  2. 隔离验证: python scripts/add_adjust_type_column.py --db <隔离库> --dry-run
  3. 正式执行: python scripts/add_adjust_type_column.py --db data/qs.db
证据: 追加 logs/repair_evidence_s2.jsonl
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

TABLE = "daily_klines"


def _has_column(conn, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({TABLE})").fetchall())


def main() -> int:
    ap = argparse.ArgumentParser(description="daily_klines adjust_type 列添加")
    ap.add_argument("--db", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--evidence", default="logs/repair_evidence_s2.jsonl")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    if args.dry_run:
        has = _has_column(conn, "adjust_type")
        total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        print(f"[dry-run] {args.db}: adjust_type 列{'已存在' if has else '不存在'}, 表行数 {total}")
        conn.close()
        return 0

    if _has_column(conn, "adjust_type"):
        print("adjust_type 列已存在, 跳过")
        return 0

    conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN adjust_type TEXT DEFAULT 'qfq'")
    conn.commit()

    dist = conn.execute(f"SELECT adjust_type, COUNT(*) FROM {TABLE} GROUP BY 1").fetchall()
    print(f"ALTER 完成, 分布: {dist}")

    evidence = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "sprint": "S2",
        "task": "F2.3_adjust_type_column",
        "db": args.db,
        "distribution": {str(k or "NULL"): v for k, v in dist},
    }
    with open(args.evidence, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

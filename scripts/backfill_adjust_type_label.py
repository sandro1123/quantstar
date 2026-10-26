#!/usr/bin/env python3
"""DL-35 daily_klines adjust_type 标记修正脚本 (F-21/D-40: 改标记不改数据)

背景: 库内 1275 万行存的是**未复权价**, 但 adjust_type 全部标记 'qfq' (前复权)。
S2 add_adjust_type_column.py 用 ALTER TABLE ... DEFAULT 'qfq' 加列, 且
repos/kline.py 的 INSERT 不含 adjust_type → 新行持续继承错误默认值。
本脚本把存量 'qfq' 改为 'raw' (与实际存储口径一致)。

只改标记, 不动价格数据 (D-40)。写库三步曲: 只读验证 → 备份 → 隔离验证 → GO。

用法:
  1. 备份: cp data/qs.db data/backups/qs_pre_adjust_label_<date>.db
  2. 隔离验证: python scripts/backfill_adjust_type_label.py --db <隔离库> --dry-run
  3. 正式执行 (需 GO): python scripts/backfill_adjust_type_label.py --db data/qs.db --apply
证据: 追加 logs/repair_evidence_dl35.jsonl
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

TABLE = "daily_klines"
NEW_LABEL = "raw"


def _has_column(conn, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({TABLE})").fetchall())


def main() -> int:
    ap = argparse.ArgumentParser(description="daily_klines adjust_type qfq→raw 标记修正")
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="真正执行 UPDATE (默认 dry-run)")
    ap.add_argument("--evidence", default="logs/repair_evidence_dl35.jsonl")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    if not _has_column(conn, "adjust_type"):
        print(f"[skip] {args.db}: adjust_type 列不存在")
        conn.close()
        return 0

    dist_before = conn.execute(
        f"SELECT adjust_type, COUNT(*) FROM {TABLE} GROUP BY 1").fetchall()
    n_qfq = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE adjust_type = 'qfq'").fetchone()[0]
    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]

    print(f"[{'APPLY' if args.apply else 'dry-run'}] {args.db}")
    print(f"  总行数 {total}, 待改 qfq 行数 {n_qfq}")
    print(f"  改前分布: {dist_before}")

    if not args.apply:
        print("  (dry-run 不写库; 加 --apply 并确保已备份后才执行)")
        conn.close()
        return 0

    conn.execute(f"UPDATE {TABLE} SET adjust_type = ? WHERE adjust_type = 'qfq'",
                 (NEW_LABEL,))
    conn.commit()

    dist_after = conn.execute(
        f"SELECT adjust_type, COUNT(*) FROM {TABLE} GROUP BY 1").fetchall()
    print(f"  改后分布: {dist_after}")

    evidence = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "card": "DL-35",
        "task": "adjust_type_label_qfq_to_raw",
        "db": args.db,
        "rows_updated": n_qfq,
        "dist_before": {str(k or "NULL"): v for k, v in dist_before},
        "dist_after": {str(k or "NULL"): v for k, v in dist_after},
    }
    with open(args.evidence, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""区间存储建表脚本 — concept_membership + plate_daily_rate

DDL 单一事实源在 src/data_layer/pipelines/concept_intervals.py (本脚本只引用,
不复制 DDL 文本, 防两处漂移)。

统筹五约束之⑤: 建表 DDL 所有列 DEFAULT 一律 NULL — DDL 常量里不写任何
DEFAULT 子句 (add_adjust_type_column.py DEFAULT 'qfq' 教训: 宁可 NULL 不可
给一个看起来合理的假值)。

🔴 生产库执行前必须另行 GO (统筹 sm-337ab3642f7f486f: worktree 内代码+DDL
脚本+读侧+单测已批, 生产建表/写入/回补单独 GO; 回补另卡 D-104 幸存者偏差)。

用法:
  1. 隔离验证: python scripts/create_concept_interval_tables.py --db <隔离库> --dry-run
  2. 正式执行 (需 GO): python scripts/create_concept_interval_tables.py --db data/qs.db
证据: 追加 logs/repair_evidence_interval_storage.jsonl
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from data_layer.pipelines.concept_intervals import (  # noqa: E402
    CONCEPT_INTERVAL_INDEXES,
    CONCEPT_MEMBERSHIP_DDL,
    PLATE_DAILY_RATE_DDL,
)

TABLES = ("concept_membership", "plate_daily_rate")


def _table_counts(conn) -> dict:
    out = {}
    for t in TABLES:
        exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (t,)).fetchone()[0]
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if exists else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="区间存储建表 (concept_membership + plate_daily_rate)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--evidence", default="logs/repair_evidence_interval_storage.jsonl")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    before = _table_counts(conn)
    if args.dry_run:
        print(f"[dry-run] {args.db}: 建表前状态 {before} "
              f"(None=表不存在); 将执行 2 张表 DDL + {len(CONCEPT_INTERVAL_INDEXES)} 个索引")
        conn.close()
        return 0

    conn.execute(CONCEPT_MEMBERSHIP_DDL)
    conn.execute(PLATE_DAILY_RATE_DDL)
    for idx_ddl in CONCEPT_INTERVAL_INDEXES:
        conn.execute(idx_ddl)
    conn.commit()

    after = _table_counts(conn)
    idx = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_mem_code'")]
    print(f"建表完成: {after}, idx_mem_code={'存在' if idx else '缺失'}")

    evidence = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "task": "interval_storage_create_tables",
        "db": args.db,
        "before": before,
        "after": after,
        "idx_mem_code": bool(idx),
    }
    os.makedirs(os.path.dirname(args.evidence) or ".", exist_ok=True)
    with open(args.evidence, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""DL-43 pipeline_runs.status 三态迁移脚本 — 放宽 CHECK 约束以容纳 'partial'

SQLite 无法 ALTER CHECK 约束, 需重建表: 建新表(含 partial) → 拷数据 → 换名。
历史行 status 只有 running/success/failed/skipped, 迁移不改任何历史值,
仅放宽约束使后续 partial 可写入。

事务安全: 全程单事务 (BEGIN IMMEDIATE ... COMMIT)。注意不得用 executescript()
建表 —— 它会先隐式 COMMIT 再执行, 打破单事务承诺, 中途失败会留下半截
pipeline_runs_new。此处用 execute() 逐条执行, 保证失败整体回滚。

用法 (写库三步曲):
  1. 备份正式库: cp data/qs.db data/backups/qs_pre_dl43_partial.db
  2. 隔离验证: python scripts/migrate_pipeline_runs_partial.py --db <隔离库> --dry-run
  3. 隔离执行: python scripts/migrate_pipeline_runs_partial.py --db <隔离库>
  4. 正式执行: python scripts/migrate_pipeline_runs_partial.py --db data/qs.db  (需 GO)
证据: 追加 logs/repair_evidence_dl43.jsonl
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

TABLE = "pipeline_runs"

# 单条 CREATE TABLE (不用 executescript, 避免隐式 COMMIT 破坏事务)
_NEW_DDL = """
CREATE TABLE pipeline_runs_new (
    run_id TEXT PRIMARY KEY,
    pipeline_name TEXT NOT NULL,
    date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed', 'skipped')),
    error_msg TEXT,
    duration_s REAL,
    version TEXT,
    config_hash TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""

_COLS = ("run_id", "pipeline_name", "date", "start_time", "end_time", "status",
         "error_msg", "duration_s", "version", "config_hash", "created_at")


def _check_allows_partial(conn) -> bool:
    """当前 CHECK 约束是否已含 'partial'。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    return bool(row and "'partial'" in row[0])


def _sample_rows(conn, n=3):
    """取首尾各 n 行作迁移前后抽样对照 (按 rowid 序, 稳定可复现)。"""
    head = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM {TABLE} ORDER BY rowid LIMIT ?", (n,)
    ).fetchall()
    tail = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM {TABLE} ORDER BY rowid DESC LIMIT ?", (n,)
    ).fetchall()
    return [dict(r) for r in list(head) + list(tail)]


def main() -> int:
    ap = argparse.ArgumentParser(description="pipeline_runs.status 放宽 CHECK 以容纳 partial")
    ap.add_argument("--db", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--evidence", default="logs/repair_evidence_dl43.jsonl")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if _check_allows_partial(conn):
        print(f"{TABLE}.status CHECK 已含 'partial', 无需迁移")
        conn.close()
        return 0

    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    dist = conn.execute(
        f"SELECT status, COUNT(*) FROM {TABLE} GROUP BY 1"
    ).fetchall()

    if args.dry_run:
        print(f"[dry-run] {args.db}: {TABLE} 行数 {total}, CHECK 未含 partial, 需重建表")
        print(f"[dry-run] 现有 status 分布: {[dict(r) for r in dist]}")
        conn.close()
        return 0

    # 迁移前抽样 (用于迁移后逐行对照)
    sample_before = _sample_rows(conn)

    # 重建表 (单事务, 失败整体回滚不留半截; execute 逐条, 不用 executescript)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_NEW_DDL)
        conn.execute(f"""
            INSERT INTO pipeline_runs_new
            ({', '.join(_COLS)})
            SELECT {', '.join(_COLS)}
            FROM {TABLE}
        """)
        moved = conn.execute("SELECT COUNT(*) FROM pipeline_runs_new").fetchone()[0]
        if moved != total:
            conn.execute("ROLLBACK")
            print(f"迁移行数不符: 源 {total} → 新 {moved}, 已回滚", file=sys.stderr)
            conn.close()
            return 1
        conn.execute(f"DROP TABLE {TABLE}")
        conn.execute(f"ALTER TABLE pipeline_runs_new RENAME TO {TABLE}")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_pipeline_date ON {TABLE}(date, pipeline_name)")
        conn.execute("COMMIT")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        print(f"迁移失败已回滚: {e}", file=sys.stderr)
        conn.close()
        return 1

    # 校验 1: 迁移后不得残留 pipeline_runs_new
    leftover = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_runs_new'"
    ).fetchone()
    if leftover:
        print("迁移后残留 pipeline_runs_new, 异常", file=sys.stderr)
        conn.close()
        return 1

    # 校验 2: 抽样逐行对照 (迁移不改任何历史值)
    sample_mismatch = 0
    for before in sample_before:
        after = conn.execute(
            f"SELECT {', '.join(_COLS)} FROM {TABLE} WHERE run_id=?",
            (before["run_id"],),
        ).fetchone()
        if after is None or dict(after) != before:
            sample_mismatch += 1
            print(f"抽样不一致: run_id={before['run_id']}", file=sys.stderr)

    # 校验 3: partial 现在可写入 (用一条临时行试写后删除)
    try:
        conn.execute(
            f"INSERT INTO {TABLE} (run_id, pipeline_name, date, start_time, status) "
            "VALUES ('__dl43_probe__', 'probe', '1970-01-01', '1970-01-01 00:00:00', 'partial')")
        conn.execute(f"DELETE FROM {TABLE} WHERE run_id='__dl43_probe__'")
        conn.commit()
        probe_ok = True
    except Exception as e:
        probe_ok = False
        print(f"partial 试写验证失败: {e}", file=sys.stderr)

    new_dist = conn.execute(
        f"SELECT status, COUNT(*) FROM {TABLE} GROUP BY 1").fetchall()
    print(f"迁移完成: {moved} 行, 抽样对照 {len(sample_before)} 行不一致 {sample_mismatch}, "
          f"partial 可写={probe_ok}, 分布={[dict(r) for r in new_dist]}")

    evidence = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "card": "DL-43",
        "task": "pipeline_runs_partial_check_migration",
        "db": args.db,
        "rows_migrated": moved,
        "sample_checked": len(sample_before),
        "sample_mismatch": sample_mismatch,
        "partial_writable": probe_ok,
        "distribution": {r["status"]: r[1] for r in new_dist},
    }
    with open(args.evidence, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    conn.close()
    return 0 if (probe_ok and sample_mismatch == 0) else 1

if __name__ == "__main__":
    sys.exit(main())

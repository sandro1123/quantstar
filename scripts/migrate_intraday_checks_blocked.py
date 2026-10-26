#!/usr/bin/env python3
"""#18 intraday_checks.status CHECK 放宽迁移 — 加 'blocked' 进白名单

背景 (审计切片C发现, 卡#18 P0):
    D1 蓝卡路径 (monitor._scan blocked 分支) 写 status='blocked',
    但表 DDL CHECK 白名单只有 ('no_action','triggered','skipped','error')
    → executemany 整批失败 → 表停摆 (末行 2026-08-28 15:00) + WARNING 每轮刷屏。

SQLite 无法 ALTER CHECK 约束, 修法照抄 DL-43 先例 (scripts/migrate_pipeline_runs_partial.py):
    建新表(白名单+blocked) → 拷数据 → 换名, 全程单事务。

事务安全 (DL-43 同款铁律):
    全程 BEGIN IMMEDIATE ... COMMIT 单事务。不得用 executescript() 建表 —
    它会先隐式 COMMIT 再执行, 打破单事务承诺, 中途失败会留下半截
    intraday_checks_new (#42 卡在册坑)。此处用 execute() 逐条执行, 保证失败整体回滚。

DDL 源已同步 (tables.py INTRA_DAY_DDL 白名单加 'blocked'), 新库重建与本迁移后一致。
历史行 status 全在旧白名单内, 迁移不改任何历史值, 仅放宽约束。

用法 (写库三步曲, 生产 ALTER 不在本卡 — 统筹亲执):
  1. 备份正式库: cp data/qs.db data/backups/qs_pre_18_blocked.db
  2. 隔离验证: python scripts/migrate_intraday_checks_blocked.py --db <隔离库> --dry-run
  3. 隔离执行: python scripts/migrate_intraday_checks_blocked.py --db <隔离库>
  4. 正式执行: python scripts/migrate_intraday_checks_blocked.py --db data/qs.db  (需 GO)
证据: 追加 logs/repair_evidence_18_blocked.jsonl
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

TABLE = "intraday_checks"

# 单条 CREATE TABLE (不用 executescript, 避免隐式 COMMIT 破坏事务 — DL-43 铁律)
# 列定义与 tables.py INTRA_DAY_DDL 完全一致, 仅 status CHECK 白名单 + 'blocked'
_NEW_DDL = """
CREATE TABLE intraday_checks_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_time TEXT NOT NULL,
    check_round TEXT NOT NULL,
    code TEXT NOT NULL,
    check_type TEXT NOT NULL,
    current_price REAL NOT NULL,
    trigger_price REAL,
    status TEXT NOT NULL CHECK (status IN ('no_action', 'triggered', 'skipped', 'error', 'blocked')),
    detail TEXT,
    latency_ms INTEGER,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

_COLS = ("id", "check_time", "check_round", "code", "check_type", "current_price",
         "trigger_price", "status", "detail", "latency_ms", "created_at")

# 迁移后必须重建的索引 (ALL_TABLES 中三张 idx_checks_*)
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_checks_time ON intraday_checks(check_time)",
    "CREATE INDEX IF NOT EXISTS idx_checks_code ON intraday_checks(code)",
    "CREATE INDEX IF NOT EXISTS idx_checks_type ON intraday_checks(check_type)",
)


def _check_allows_blocked(conn) -> bool:
    """当前 CHECK 约束是否已含 'blocked' (幂等探测)。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    return bool(row and "'blocked'" in row[0])


def _sample_rows(conn, n=3):
    """取首尾各 n 行作迁移前后抽样对照 (按 rowid 序, 稳定可复现 — DL-43 同款)。"""
    head = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM {TABLE} ORDER BY rowid LIMIT ?", (n,)
    ).fetchall()
    tail = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM {TABLE} ORDER BY rowid DESC LIMIT ?", (n,)
    ).fetchall()
    ids = {r["id"] for r in head}
    merged = list(head) + [r for r in tail if r["id"] not in ids]
    return [dict(r) for r in merged]


def main() -> int:
    ap = argparse.ArgumentParser(description="intraday_checks.status CHECK 放宽以容纳 blocked")
    ap.add_argument("--db", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--evidence", default="logs/repair_evidence_18_blocked.jsonl")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if _check_allows_blocked(conn):
        print(f"{TABLE}.status CHECK 已含 'blocked', 无需迁移")
        conn.close()
        return 0

    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    if not exists:
        print(f"{TABLE} 不存在 (全新库, start() 会用新 DDL 建表), 无需迁移")
        conn.close()
        return 0

    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    dist = conn.execute(
        f"SELECT status, COUNT(*) FROM {TABLE} GROUP BY 1").fetchall()

    if args.dry_run:
        print(f"[dry-run] {args.db}: {TABLE} 行数 {total}, CHECK 未含 blocked, 需重建表")
        print(f"[dry-run] 现有 status 分布: {[dict(r) for r in dist]}")
        conn.close()
        return 0

    # 迁移前抽样 (用于迁移后逐行对照)
    sample_before = _sample_rows(conn)

    # 重建表 (单事务, 失败整体回滚不留半截; execute 逐条, 不用 executescript)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS intraday_checks_new")
        conn.execute(_NEW_DDL)
        conn.execute(f"""
            INSERT INTO intraday_checks_new
            ({', '.join(_COLS)})
            SELECT {', '.join(_COLS)}
            FROM {TABLE}
        """)
        moved = conn.execute("SELECT COUNT(*) FROM intraday_checks_new").fetchone()[0]
        if moved != total:
            conn.execute("ROLLBACK")
            print(f"迁移行数不符: 源 {total} → 新 {moved}, 已回滚", file=sys.stderr)
            conn.close()
            return 1
        conn.execute(f"DROP TABLE {TABLE}")
        conn.execute(f"ALTER TABLE intraday_checks_new RENAME TO {TABLE}")
        for idx in _INDEXES:
            conn.execute(idx)
        conn.execute("COMMIT")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        print(f"迁移失败已回滚: {e}", file=sys.stderr)
        conn.close()
        return 1

    # 校验 1: 迁移后不得残留 intraday_checks_new
    leftover = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='intraday_checks_new'"
    ).fetchone()
    if leftover:
        print("迁移后残留 intraday_checks_new, 异常", file=sys.stderr)
        conn.close()
        return 1

    # 校验 2: 抽样逐行对照 (迁移不改任何历史值)
    sample_mismatch = 0
    for before in sample_before:
        after = conn.execute(
            f"SELECT {', '.join(_COLS)} FROM {TABLE} WHERE id=?",
            (before["id"],),
        ).fetchone()
        if after is None or dict(after) != before:
            sample_mismatch += 1
            print(f"抽样不一致: id={before['id']}", file=sys.stderr)

    # 校验 3: blocked 现在可写入 (试写一行后删除) + 旧非法值仍拒
    probe_ok = reject_ok = False
    try:
        conn.execute(
            f"INSERT INTO {TABLE} (check_time, check_round, code, check_type, "
            "current_price, status) "
            "VALUES ('1970-01-01 00:00:00', 'R0', '__18probe__', 'entry', 0.0, 'blocked')")
        conn.execute(f"DELETE FROM {TABLE} WHERE code='__18probe__'")
        conn.commit()
        probe_ok = True
    except Exception as e:
        print(f"blocked 试写验证失败: {e}", file=sys.stderr)
    try:
        conn.execute(
            f"INSERT INTO {TABLE} (check_time, check_round, code, check_type, "
            "current_price, status) "
            "VALUES ('1970-01-01 00:00:00', 'R0', '__18probe2__', 'entry', 0.0, 'bogus_v5')")
        conn.execute(f"DELETE FROM {TABLE} WHERE code='__18probe2__'")
        conn.commit()
        print("非法值 bogus_v5 竟被接受, CHECK 失守", file=sys.stderr)
    except sqlite3.IntegrityError:
        conn.rollback()
        reject_ok = True

    new_dist = conn.execute(
        f"SELECT status, COUNT(*) FROM {TABLE} GROUP BY 1").fetchall()
    print(f"迁移完成: {moved} 行, 抽样对照 {len(sample_before)} 行不一致 {sample_mismatch}, "
          f"blocked 可写={probe_ok}, 非法值仍拒={reject_ok}, "
          f"分布={[dict(r) for r in new_dist]}")

    evidence = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "card": "#18",
        "task": "intraday_checks_blocked_check_migration",
        "db": args.db,
        "rows_migrated": moved,
        "sample_checked": len(sample_before),
        "sample_mismatch": sample_mismatch,
        "blocked_writable": probe_ok,
        "bogus_rejected": reject_ok,
        "distribution": {r["status"]: r[1] for r in new_dist},
    }
    evidence_path = Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with open(evidence_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    conn.close()
    return 0 if (probe_ok and reject_ok and sample_mismatch == 0) else 1


if __name__ == "__main__":
    sys.exit(main())

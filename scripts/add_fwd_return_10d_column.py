#!/usr/bin/env python3
"""GO-F T2: factor_daily 一次 ALTER 补齐 FWD_HORIZON_COLS 全部缺失列 — DDL 脚本。

GO-F 批次扩展 (统筹 sm-ac2f89da + 用户验收线三窗口 1/3/5 日): 单一事实源已含
fwd_return_3d (FWD_HORIZONS=(1,3,5,10,20)), 本脚本对每个 horizon 列做幂等补齐,
当前实际缺口 = fwd_return_3d + fwd_return_10d。列建好后 backfill 历史脚本一次
扫表同时回补全周期。

设计要点:
- DDL 单一事实源: 列名/表名引 data_layer._loader.FWD_HORIZONS+FWD_HORIZON_COLS,
  不复制字面量; 未来再加周期只需改 _loader.py, 本脚本零改动自动跟进。
- 无 DEFAULT 子句 (统筹约束): 前向收益列无业务默认值, DEFAULT 会掩盖"未回填"
  与"真实值"的区别; PRAGMA 验 dflt_value 为 NULL 作为 schema 层证据。
- --dry-run 默认零写入 / --apply 显式。
- 幂等: 已存在的列逐个跳过。
- flock 单实例 (铁律 #4 串行写库)。
- 证据三件套: 前后 PRAGMA 对比 + 行数不变 + jsonl 追加。

用法 (GO 后):
    cp data/qs.db data/backups/qs_pre_fwd_cols.db        # 先备份
    PYTHONPATH=src .venv/bin/python scripts/add_fwd_return_10d_column.py --db <隔离库> --dry-run
    PYTHONPATH=src .venv/bin/python scripts/add_fwd_return_10d_column.py --db data/qs.db --apply
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from data_layer._loader import FWD_HORIZONS, FWD_HORIZON_COLS  # noqa: E402

TABLE = "factor_daily"
COLUMNS = [FWD_HORIZON_COLS[h] for h in FWD_HORIZONS]  # 全周期列 — 单一事实源引用


def _columns(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(f"PRAGMA table_info({TABLE})").fetchall()


def _existing_cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in _columns(conn)}


def _emit(log_path: str, event: str, **payload) -> None:
    record = {"event": event, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), **payload}
    line = json.dumps(record, ensure_ascii=False)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[fwdddl][LOG-FAILED] {e}", file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="factor_daily 加 fwd_return_10d 列")
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="显式执行 ALTER (默认 dry-run 零写入)")
    ap.add_argument("--evidence", default="logs/fwd10d_ddl_evidence.jsonl")
    args = ap.parse_args()

    lock_path = os.path.join(PROJECT_ROOT, "logs", "fwd10d_ddl.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[fwdddl] 已有实例在运行 (flock 占用), 退出。", file=sys.stderr)
        return 3

    if not os.path.exists(args.db):
        print(f"[fwdddl] 库不存在: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    try:
        cols_before = [r[1] for r in _columns(conn)]
        total_rows = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]

        existing = _existing_cols(conn)
        missing = [c for c in COLUMNS if c not in existing]
        if not args.apply:
            print(f"[dry-run] {args.db}: {TABLE} 行数={total_rows}")
            print(f"[dry-run] 现有 fwd 列: {[c for c in cols_before if c.startswith('fwd_return_')]}")
            print(f"[dry-run] 待补缺失列: {missing or '无 (全部在档)'}")
            print("[dry-run] 零写入。确认后加 --apply 执行 (先备份原库)。")
            conn.close()
            return 0

        if not missing:
            print("fwd 列已全部存在, 幂等跳过")
            conn.close()
            return 0

        added: list[str] = []
        for col in missing:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col} REAL")  # 无 DEFAULT 子句
            added.append(col)
        conn.commit()

        cols_after = [r[1] for r in _columns(conn)]
        metas = {r[1]: r for r in _columns(conn)}
        rows_after = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]

        # schema 层实证 (D 会话手法): 新列不得带 DEFAULT
        for col in added:
            meta = metas.get(col)
            assert meta is not None and meta[4] is None, \
                f"dflt_value 应为 None, 实际 {meta}"
        assert total_rows == rows_after, "ALTER 改变了行数?!"

        null_counts = {col: conn.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE {col} IS NULL").fetchone()[0]
            for col in added}

        _emit(args.evidence, "ddl_applied",
              db=args.db, table=TABLE, columns_added=added,
              dflt_values={c: metas[c][4] for c in added},
              row_count=rows_after, null_counts=null_counts,
              cols_before=len(cols_before), cols_after=len(cols_after))
        print(f"[fwdddl] ALTER 完成: {TABLE} 加列 {added} REAL (无 DEFAULT)。"
              f"行数 {rows_after} 不变, 新列全 NULL 待 T3 回填。")
        return 0
    except Exception as e:
        _emit(args.evidence, "ddl_failed", db=args.db, error=str(e))
        raise
    finally:
        conn.close()
        lock_file.close()


if __name__ == "__main__":
    sys.exit(main())

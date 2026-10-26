"""cleanup_pipeline_runs_noise.py — DL-13 清理 pipeline_runs 高频噪音记录

api_health_check 每 5 分钟经 run_guarded 写一条 pipeline_runs, 累计数百条噪音,
淹没真实管线运行记录。本脚本清理这些噪音行 (DL-13 噪音分流的存量清理侧)。

写库三步曲 (铁律): 默认 dry-run 只报告不删; --apply 才实际 DELETE。
生产执行须用户 GO + 备份。提供 --backup 先落 pipeline_runs_noise_backup 表。
"""
from __future__ import annotations

import argparse
import sqlite3

from common.logging import get_logger

logger = get_logger("ops.cleanup_pipeline_runs_noise")

DEFAULT_NOISE_NAMES = ("api_health_check",)
BACKUP_TABLE = "pipeline_runs_noise_backup"


def count_noise(conn, names=DEFAULT_NOISE_NAMES) -> dict:
    """统计各噪音 pipeline_name 的行数 {name: count}。"""
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"SELECT pipeline_name, COUNT(*) AS n FROM pipeline_runs "
        f"WHERE pipeline_name IN ({placeholders}) GROUP BY pipeline_name",
        tuple(names),
    ).fetchall()
    return {r["pipeline_name"]: r["n"] for r in rows}


def cleanup(conn, names=DEFAULT_NOISE_NAMES, apply=False, backup=False) -> dict:
    """清理噪音行。apply=False 仅统计; backup=True 先落备份表。"""
    stats = count_noise(conn, names)
    total = sum(stats.values())
    if total == 0:
        logger.info("no noise rows found")
        return {"found": stats, "deleted": 0, "applied": bool(apply)}
    if not apply:
        logger.info(f"[dry-run] would delete {total} noise rows: {stats}")
        return {"found": stats, "deleted": 0, "applied": False}

    placeholders = ",".join("?" for _ in names)
    if backup:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} AS "
            f"SELECT * FROM pipeline_runs WHERE pipeline_name IN ({placeholders})",
            tuple(names),
        )
        logger.info(f"backup table {BACKUP_TABLE} created")
    cur = conn.execute(
        f"DELETE FROM pipeline_runs WHERE pipeline_name IN ({placeholders})",
        tuple(names),
    )
    conn.commit()
    logger.info(f"deleted {cur.rowcount} noise rows")
    return {"found": stats, "deleted": cur.rowcount, "applied": True}


def main():
    parser = argparse.ArgumentParser(description="DL-13 pipeline_runs 噪音清理")
    parser.add_argument("--db", default="data/qs.db", help="qs.db 路径")
    parser.add_argument("--apply", action="store_true", help="实际删除 (默认 dry-run)")
    parser.add_argument("--backup", action="store_true", help="apply 前先落备份表")
    parser.add_argument("--names", nargs="+", default=list(DEFAULT_NOISE_NAMES),
                        help="视为噪音的 pipeline_name 列表")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        stats = cleanup(conn, names=tuple(args.names), apply=args.apply, backup=args.backup)
    finally:
        conn.close()
    print(stats)


if __name__ == "__main__":
    main()

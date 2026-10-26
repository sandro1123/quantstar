"""backfill_pre_close.py — DL-14 (Part 1) pre_close=0 历史回填

F-15: daily_klines 有 11,150 行 pre_close=0 (跨 1826 个交易日), 最大批
2026-07-29 有 5180 行。pre_close=0 会污染涨跌幅/复权计算。

回填逻辑: 对每行 pre_close=0, 取该股当日之前最近一个有效 close (>0) 回填。
实测可行: 2026-07-29 批 5180/5180 均有前序 close 可用。

写库三步曲 (铁律): 本脚本默认 dry-run 只报告不写; --apply 才实际 UPDATE。
生产执行须用户 GO + 备份。提供 --backup 先落 pre_close_backup 表。
"""

from __future__ import annotations

import argparse

from common.logging import get_logger

logger = get_logger("data.backfill_pre_close")

# 回填 SQL: 用相关子查询取当日之前最近的有效 close
_BACKFILL_SELECT = """
    SELECT a.code, a.date, a.pre_close AS old_pre_close,
           (SELECT b.close FROM daily_klines b
            WHERE b.code = a.code AND b.date < a.date AND b.close > 0
            ORDER BY b.date DESC LIMIT 1) AS new_pre_close
    FROM daily_klines a
    WHERE a.pre_close = 0
"""


def find_backfillable(db) -> list[dict]:
    """返回可回填行 [{code, date, old_pre_close, new_pre_close}]。

    new_pre_close 为 None 表示无前序 close (无法回填, 跳过)。
    """
    conn = db.get_connection()
    rows = conn.execute(_BACKFILL_SELECT).fetchall()
    result = []
    for r in rows:
        result.append({
            "code": r["code"], "date": r["date"],
            "old_pre_close": r["old_pre_close"],
            "new_pre_close": r["new_pre_close"],
        })
    return result


def backfill(db, apply: bool = False, backup: bool = False) -> dict:
    """执行回填。apply=False 仅统计; backup=True 先落备份表。

    返回 {total_zero, backfillable, skipped_no_prior, applied}。
    """
    conn = db.get_connection()
    rows = find_backfillable(db)
    total_zero = len(rows)
    backfillable = [r for r in rows if r["new_pre_close"] is not None]
    skipped = total_zero - len(backfillable)

    stats = {
        "total_zero": total_zero,
        "backfillable": len(backfillable),
        "skipped_no_prior": skipped,
        "applied": 0,
    }
    if not apply:
        logger.info(
            f"[dry-run] pre_close=0 共 {total_zero} 行, 可回填 {len(backfillable)}, "
            f"无前序close跳过 {skipped}")
        return stats

    if backup:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pre_close_backup AS
            SELECT code, date, pre_close FROM daily_klines WHERE pre_close = 0
        """)
        conn.commit()
        logger.info("backup table pre_close_backup created")

    for r in backfillable:
        conn.execute(
            "UPDATE daily_klines SET pre_close = ? WHERE code = ? AND date = ?",
            (r["new_pre_close"], r["code"], r["date"]),
        )
        stats["applied"] += 1
    conn.commit()
    logger.info(f"applied {stats['applied']} pre_close backfills")
    return stats


def main():
    parser = argparse.ArgumentParser(description="DL-14 pre_close=0 回填")
    parser.add_argument("--db", default=None, help="qs.db 路径 (默认 Database())")
    parser.add_argument("--apply", action="store_true",
                        help="实际执行 UPDATE (默认 dry-run)")
    parser.add_argument("--backup", action="store_true",
                        help="apply 前先落 pre_close_backup 表")
    args = parser.parse_args()

    if args.db:
        from common.database import Database
        db = Database(args.db)
    else:
        from common.database import Database
        db = Database()

    stats = backfill(db, apply=args.apply, backup=args.backup)
    print(stats)


if __name__ == "__main__":
    main()

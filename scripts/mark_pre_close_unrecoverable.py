#!/usr/bin/env python3
"""DL-38 pre_close=0 不可回补标记脚本 — 复用 DL-29 data_quality_flags 机制

背景: daily_klines 有 5969 行 pre_close=0, 全部物理不可回补 (实测 0 行有前序
close>0)。成因三分类 (实测 2026-08-22):
  - ipo_day          3606 行: 该行即上市首日 (date == stock_info.list_date)
  - collection_start 2107 行: 该股在库首行但上市更早 (采集起点, 前序未入库)
  - no_list_date      256 行: stock_info 无 list_date

标记方式选 b (复用 DL-29 FlagStore 的 data_quality_flags 独立表), 不 ALTER
12.75 万行级 daily_klines (quality.py docstring 明令禁止), 不另建第二套机制。
flag='unrecoverable' (区别于写入时的 'suspicious'/'outlier'), rule 记成因。

用法 (写库三步曲, 默认 dry-run, --apply 需统筹 GO):
  1. 备份正式库: cp data/qs.db data/backups/qs_pre_dl38_mark.db
  2. 隔离验证: python scripts/mark_pre_close_unrecoverable.py --db <隔离库> --dry-run
  3. 隔离执行: python scripts/mark_pre_close_unrecoverable.py --db <隔离库> --apply
  4. 正式执行: python scripts/mark_pre_close_unrecoverable.py --db data/qs.db --apply  (需 GO)
证据: 追加 logs/repair_evidence_dl38.jsonl
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime

_FLAGS_DDL = """
CREATE TABLE IF NOT EXISTS data_quality_flags (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    table_name TEXT NOT NULL DEFAULT 'daily_klines',
    field TEXT NOT NULL,
    flag TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    rule TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    PRIMARY KEY (code, date, table_name, field, rule)
);
"""

RULE = "pre_close_unrecoverable"


def classify(conn) -> dict:
    """返回 {cause: [(code, date, reason), ...]}。只读。"""
    cur = conn.cursor()
    # 每行 pre_close=0 的成因: 与 stock_info.list_date 及该股在库首行比对
    cur.execute("""
        WITH bad AS (
            SELECT code, date FROM daily_klines
            WHERE pre_close = 0 OR pre_close IS NULL
        ),
        firsts AS (
            SELECT code, MIN(date) AS min_date FROM daily_klines GROUP BY code
        )
        SELECT b.code, b.date, s.list_date,
               CASE WHEN b.date = f.min_date THEN 1 ELSE 0 END AS is_first_row
        FROM bad b
        JOIN firsts f ON b.code = f.code
        LEFT JOIN stock_info s ON b.code = s.code
    """)
    out = {"ipo_day": [], "collection_start": [], "no_list_date": [], "other": []}
    for code, date, list_date, is_first_row in cur.fetchall():
        ld = (list_date or "").strip()
        compact = date.replace("-", "")
        if not is_first_row:
            # 理论上不存在 (实测 0 行), 防御性归类
            out["other"].append((code, date, f"non-first-row, list_date={ld!r}"))
        elif not ld:
            out["no_list_date"].append((code, date, "stock_info 无 list_date, 无法判定首日"))
        elif compact == ld:
            out["ipo_day"].append((code, date, f"上市首日 (list_date={ld})"))
        else:
            out["collection_start"].append(
                (code, date, f"在库首行但上市更早 (list_date={ld}), 采集起点前序未入库"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="pre_close=0 不可回补标记 (复用 DL-29 flag 表)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="实际写入标记 (默认 dry-run; 生产执行需统筹 GO)")
    ap.add_argument("--dry-run", action="store_true",
                    help="显式 dry-run (与默认行为一致, 仅为用法清晰)")
    ap.add_argument("--evidence", default="logs/repair_evidence_dl38.jsonl")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    causes = classify(conn)
    total = sum(len(v) for v in causes.values())
    print(f"pre_close=0/NULL 分类 (共 {total} 行):")
    for cause, rows in causes.items():
        print(f"  {cause}: {len(rows)}")
        for code, date, reason in rows[:3]:
            print(f"      {code} {date} — {reason}")

    # 一致性自检: 全部应为不可回补 (不应有可回补的漏网)
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM daily_klines k
        WHERE (k.pre_close = 0 OR k.pre_close IS NULL)
        AND EXISTS (SELECT 1 FROM daily_klines p
                    WHERE p.code = k.code AND p.date < k.date AND p.close > 0)
    """)
    backfillable = cur.fetchone()[0]
    print(f"可回补漏网行数 (应为 0): {backfillable}")
    if backfillable:
        print("⚠️ 存在可回补行, 不应全部标 unrecoverable — 中止", file=sys.stderr)
        conn.close()
        return 1

    if not args.apply:
        print("[dry-run] 未写入。--apply 需统筹 GO (写库三步曲)。")
        conn.close()
        return 0

    # --- 实际写入 (需 GO) ---
    conn.executescript(_FLAGS_DDL)
    written = 0
    for cause, rows in causes.items():
        for code, date, reason in rows:
            conn.execute(
                "INSERT OR IGNORE INTO data_quality_flags "
                "(code, date, table_name, field, flag, reason, rule) "
                "VALUES (?, ?, 'daily_klines', 'pre_close', 'unrecoverable', ?, ?)",
                (code, date, f"{cause}: {reason}", RULE))
            written += 1
    conn.commit()

    # 验证
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM data_quality_flags WHERE rule = ?", (RULE,))
    flagged = cur.fetchone()[0]
    print(f"已写入标记 {written} 行, data_quality_flags 中 rule={RULE} 共 {flagged} 行")

    evidence = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "card": "DL-38",
        "task": "pre_close_unrecoverable_mark",
        "db": args.db,
        "total": total,
        "causes": {k: len(v) for k, v in causes.items()},
        "backfillable_leak": backfillable,
        "flagged_rows": flagged,
    }
    with open(args.evidence, "a", encoding="utf-8") as f:
        f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

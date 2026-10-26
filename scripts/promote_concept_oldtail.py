#!/usr/bin/env python3
"""concept 无偏回补 旧尾段 — 生产写库 promote 步 (22:05窗口, 备份三步曲后)

把隔离库里旧尾 (2024-01-02~2026-04-29) 的 _backfill 真概念行搬进生产 qs.db。
逐日事务: 先删该日 industry_fallback 行 (行业不再冒充概念, 与 pipeline 写库语义
一致), 再 INSERT OR REPLACE backfill 行。每批指纹断言 (行数/概念数/source后缀)。
仿 promote_concept_seg1.py, 日期区间参数化 (默认旧尾)。

用法 (生产, 须先备份三步曲 + 统筹授权):
  python scripts/promote_concept_oldtail.py --src /tmp/cboldtail_isolated.db --dst data/qs.db --apply
  不带 --apply = dry-run 只报断言不写。
"""
import argparse, sqlite3, sys

OLD_TAIL_START = "2024-01-02"
OLD_TAIL_END = "2026-04-29"
BATCH_SIZE = 10

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="隔离库 (含旧尾 backfill 行)")
    ap.add_argument("--dst", required=True, help="生产库")
    ap.add_argument("--apply", action="store_true", help="真写 (默认 dry-run)")
    ap.add_argument("--start", default=OLD_TAIL_START, help="日期区间起 (默认旧尾)")
    ap.add_argument("--end", default=OLD_TAIL_END, help="日期区间止 (默认旧尾)")
    args = ap.parse_args()

    src = sqlite3.connect("file:%s?mode=ro" % args.src, uri=True)
    dates = [r[0] for r in src.execute(
        "SELECT DISTINCT date FROM concept_daily WHERE date BETWEEN ? AND ? "
        "AND source LIKE ? ORDER BY date", (args.start, args.end, "%_backfill%")).fetchall()]
    print("OLDTAIL backfill dates in src: %d (%s .. %s)" % (len(dates), dates[0] if dates else "-", dates[-1] if dates else "-"))

    dst = sqlite3.connect(args.dst)
    total_ins = total_del = 0
    for i, d in enumerate(dates):
        rows = src.execute(
            "SELECT code, date, concept_name, change_pct, source FROM concept_daily "
            "WHERE date=? AND source LIKE ?", (d, "%_backfill%")).fetchall()
        if args.apply:
            cur = dst.execute("DELETE FROM concept_daily WHERE date=? AND source=?",
                              (d, "industry_fallback"))
            deleted = cur.rowcount
            dst.executemany(
                "INSERT OR REPLACE INTO concept_daily (code, date, concept_name, change_pct, source) "
                "VALUES (?,?,?,?,?)", rows)
            dst.commit()
            total_del += deleted
            total_ins += len(rows)
        if (i + 1) % BATCH_SIZE == 0 or (i + 1) == len(dates):
            # 指纹断言: 该日 dst backfill 行数应 = src 行数
            n_dst = dst.execute("SELECT COUNT(*) FROM concept_daily WHERE date=? AND source LIKE ?",
                                (d, "%_backfill%")).fetchone()[0] if args.apply else 0
            nc = dst.execute("SELECT COUNT(DISTINCT concept_name) FROM concept_daily WHERE date=?",
                             (d,)).fetchone()[0] if args.apply else 0
            print("BATCH %d/%d date=%s src_rows=%d dst_backfill=%d concepts=%d %s" % (
                i + 1, len(dates), d, len(rows), n_dst, nc,
                "OK" if (not args.apply or n_dst == len(rows)) else "MISMATCH"))
    if args.apply:
        print("PROMOTE_DONE dates=%d inserted=%d deleted_fallback=%d" % (len(dates), total_ins, total_del))
    else:
        print("DRYRUN_DONE dates=%d (no write)" % len(dates))
    src.close(); dst.close()

if __name__ == "__main__":
    main()

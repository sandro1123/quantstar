"""P-DATA-A1 存量 change_pct 重算 — 基于同复权口径前一复权日 close。

背景: 数据源 pre_close 缺失/不复权, 导致 change_pct 大量失真
(99.78% 行 pre_close=0, |change_pct| 超标 8万+ 行)。
本脚本用库内"前一复权日 close"重算 change_pct 与 pre_close。

遵循数据操作安全规范:
1. --dry-run 只读核对(默认)
2. --apply 前先备份受影响行到 CSV
3. --apply 写正式库需显式确认

用法:
  python3 scripts/recompute_change_pct.py --dry-run
  python3 scripts/recompute_change_pct.py --apply
"""
import argparse
import csv
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "qs.db"
BACKUP_DIR = Path(__file__).resolve().parent.parent / "data" / "backups"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA busy_timeout=120000")
    con.execute("PRAGMA journal_mode=WAL")

    print("=== 重算前统计 ===")
    total = con.execute("SELECT COUNT(*) FROM daily_klines").fetchone()[0]
    bad = con.execute(
        "SELECT COUNT(*) FROM daily_klines WHERE pre_close=0 AND volume>0"
    ).fetchone()[0]
    print(f"  总行 {total}, pre_close=0 且 volume>0: {bad}")

    print("\n=== 按 code 逐只重算(干跑) ===")
    codes = [r[0] for r in con.execute("SELECT DISTINCT code FROM daily_klines")]
    print(f"  股票数 {len(codes)}")

    fixed_rows = 0
    new_bad_rows = 0
    t0 = time.time()
    if not args.apply:
        # 干跑: 仅对抽样股票验证可修复行数
        sample = codes[:50]
        for code in sample:
            rows = con.execute(
                "SELECT date, close, pre_close, change_pct FROM daily_klines "
                "WHERE code=? AND close>0 ORDER BY date",
                (code,),
            ).fetchall()
            prev_close = 0.0
            for d, close, pclose, cpct in rows:
                if prev_close > 0:
                    new_cpct = round((close - prev_close) / prev_close * 100, 2)
                    if cpct != new_cpct:
                        fixed_rows += 1
                prev_close = close
        print(f"  抽样 {len(sample)} 只, 将修复 change_pct 行数: {fixed_rows}")
        print(f"  全量预估: ~{int(fixed_rows / len(sample) * len(codes))} 行")
        print("\n(dry-run, 加 --apply 才会写入; --apply 前会先备份)")
        con.close()
        return

    # ---- apply: 先备份 ----
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_csv = BACKUP_DIR / f"daily_klines_change_pct_before_{ts}.csv.gz"
    print(f"\n备份到 {backup_csv} ...")
    import gzip
    with gzip.open(backup_csv, "wt", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["code", "date", "old_pre_close", "old_change_pct"])
        for code in codes:
            for d, pclose, cpct in con.execute(
                "SELECT date, pre_close, change_pct FROM daily_klines WHERE code=?",
                (code,),
            ):
                writer.writerow([code, d, pclose, cpct])
    print(f"  备份完成 ({backup_csv.stat().st_size} bytes)")

    # ---- 重算 ----
    resp = input(f"\n确认写正式库并重算 {len(codes)} 只的 change_pct? [y/N] ")
    if resp.strip().lower() != "y":
        print("已取消")
        sys.exit(1)

    with con:
        for code in codes:
            rows = con.execute(
                "SELECT date, close FROM daily_klines WHERE code=? AND close>0 ORDER BY date",
                (code,),
            ).fetchall()
            prev_close = 0.0
            for d, close in rows:
                if prev_close > 0:
                    new_cpct = round((close - prev_close) / prev_close * 100, 2)
                    con.execute(
                        "UPDATE daily_klines SET change_pct=?, pre_close=? "
                        "WHERE code=? AND date=?",
                        (new_cpct, prev_close, code, d),
                    )
                    fixed_rows += 1
                prev_close = close
            new_bad_rows += 1
            if new_bad_rows % 500 == 0:
                print(f"  已处理 {new_bad_rows}/{len(codes)} 只 ({time.time()-t0:.0f}s)")

    print(f"\n✅ 重算完成: {fixed_rows} 行 change_pct 已修正, 用时 {time.time()-t0:.0f}s")
    print(f"备份文件: {backup_csv}")
    con.close()


if __name__ == "__main__":
    main()

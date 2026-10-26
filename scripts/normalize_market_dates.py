"""P-DATA-A2/A4 存量数据规范化 — market + 估值日期格式。

遵循数据操作安全规范:
1. 只读核对先行
2. 先在隔离副本验证
3. 写正式库需 --apply + 用户确认

用法:
  python3 scripts/normalize_market_dates.py --dry-run   # 只读核对,打印将变更行
  python3 scripts/normalize_market_dates.py --apply     # 写正式库(需显式确认)

修复:
  A2: stock_info.market 7种口径 -> SH/SZ/BJ (按 code 前缀推导)
  A4: stock_valuation.date 紧凑格式(20260717) -> ISO(2026-07-17)
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from data_layer.repos.stock import normalize_market

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "qs.db"


def normalize_date(v: str) -> str:
    """紧凑 YYYYMMDD -> ISO YYYY-MM-DD; 已是 ISO 则原样返回"""
    v = (v or "").strip()
    if len(v) == 8 and v.isdigit():
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}"
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply_mode = args.apply

    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA busy_timeout=60000")

    # ---- A2: market 规范化 ----
    rows = con.execute(
        "SELECT code, market FROM stock_info WHERE code GLOB '[0-9]*'"
    ).fetchall()
    market_changes = []
    for code, mkt in rows:
        new = normalize_market(code, mkt)
        if new != mkt:
            market_changes.append((code, mkt, new))

    # ---- A4: 估值日期格式 ----
    date_rows = con.execute(
        "SELECT code, date FROM stock_valuation"
    ).fetchall()
    date_changes = []
    for code, d in date_rows:
        nd = normalize_date(d)
        if nd != d:
            date_changes.append((code, d, nd))

    print(f"=== A2 market 规范化 ===")
    print(f"需变更 {len(market_changes)} 行")
    from collections import Counter
    from_pairs = Counter((o, n) for _, o, n in market_changes)
    for (o, n), cnt in sorted(from_pairs.items()):
        print(f"  {o!r:10s} -> {n!r:4s}  x{cnt}")
    print()
    print(f"=== A4 估值日期格式 ===")
    print(f"需变更 {len(date_changes)} 行")
    if date_changes:
        print(f"  示例: {date_changes[0][1]} -> {date_changes[0][2]}")
        print(f"  末尾: {date_changes[-1][1]} -> {date_changes[-1][2]}")

    if apply_mode:
        # 显式确认
        resp = input(f"\n确认写入正式库? {len(market_changes)}行market + {len(date_changes)}行日期 [y/N] ")
        if resp.strip().lower() != "y":
            print("已取消")
            sys.exit(1)
        with con:
            for code, _, new in market_changes:
                con.execute(
                    "UPDATE stock_info SET market=? WHERE code=?", (new, code)
                )
            for code, old_d, nd in date_changes:
                con.execute(
                    "UPDATE stock_valuation SET date=? WHERE code=? AND date=?",
                    (nd, code, old_d),
                )
        print(f"✅ 已写入: market {len(market_changes)}行, 估值日期 {len(date_changes)}行")
    else:
        print("\n(dry-run, 加 --apply 才会写入)")

    con.close()


if __name__ == "__main__":
    main()

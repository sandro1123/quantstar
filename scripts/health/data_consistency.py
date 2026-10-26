#!/usr/bin/env python3
"""新旧系统数据一致性检查

对比 ~/qs/data/qs.db 和 ~/quantstar/data/stocks_full.db 的核心数据表。
用法: python scripts/health/data_consistency.py [--date DATE]
"""

import os
import sys
import sqlite3
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))


def get_old_db_path() -> str:
    candidates = [
        os.path.expanduser("~/quantstar/data/stocks_full.db"),
        os.path.expanduser("~/quantstar_test/data/stocks_full.db"),
        "data/stocks_full.db",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""


def format_row(row) -> dict:
    return dict(row) if row else {}


def compare_klines(old_conn, new_conn, date: str) -> dict:
    old_date = date.replace("-", "")
    old = old_conn.execute(
        "SELECT code, open, high, low, close, volume FROM daily_klines WHERE date = ?",
        (old_date,),
    ).fetchall()
    old_set = {r["code"]: r for r in old}

    new = new_conn.execute(
        "SELECT code, open, high, low, close, volume FROM daily_klines WHERE date = ?",
        (date,),
    ).fetchall()
    new_set = {r["code"]: r for r in new}

    old_codes = set(old_set.keys())
    new_codes = set(new_set.keys())
    common = old_codes & new_codes

    mismatches = 0
    for code in common:
        o = old_set[code]
        n = new_set[code]
        if abs((o["close"] or 0) - (n["close"] or 0)) > 0.01:
            mismatches += 1
            if mismatches <= 3:
                print(f"  ⚠ {code}: old_close={o['close']}, new_close={n['close']}")

    return {
        "table": "daily_klines",
        "date": date,
        "old_count": len(old),
        "new_count": len(new),
        "common_count": len(common),
        "only_old": len(old_codes - new_codes),
        "only_new": len(new_codes - old_codes),
        "close_mismatch": mismatches,
        "status": "OK" if mismatches == 0 else "DIFF",
    }


def check(args):
    date = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    old_db = args.old_db or get_old_db_path()
    if not old_db or not os.path.exists(old_db):
        print(f"旧数据库未找到: {old_db}")
        sys.exit(1)

    new_db = args.new_db or "data/qs.db"
    print(f"检查日期: {date}")
    print(f"旧数据库: {old_db}")
    print(f"新数据库: {new_db}")
    print()

    old_conn = sqlite3.connect(old_db)
    old_conn.row_factory = sqlite3.Row
    new_conn = sqlite3.connect(new_db)
    new_conn.row_factory = sqlite3.Row

    results = []
    results.append(compare_klines(old_conn, new_conn, date))

    # Summary
    print()
    print("=" * 60)
    print(f"一致性检查结果 ({date}):")
    print("=" * 60)
    all_ok = True
    for r in results:
        status = "✅" if r["status"] == "OK" else "⚠️"
        if r["status"] != "OK":
            all_ok = False
        print(f"  {status} {r['table']}: old={r['old_count']}, new={r['new_count']}, "
              f"common={r['common_count']}, diff={r['close_mismatch']}")

    old_conn.close()
    new_conn.close()

    if all_ok:
        print(f"\n✅ 数据一致 ({date})")
    else:
        print(f"\n⚠️ 数据存在差异 ({date})")

    return all_ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="新旧系统数据一致性检查")
    parser.add_argument("--date", help="检查日期 (默认昨天)")
    parser.add_argument("--old-db", help="旧数据库路径")
    parser.add_argument("--new-db", default="data/qs.db", help="新数据库路径")
    args = parser.parse_args()
    check(args)
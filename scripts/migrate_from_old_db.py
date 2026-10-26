#!/usr/bin/env python3
"""从旧 stocks_full.db 迁移到新系统分层 Repository 格式

用法: python scripts/migrate_from_old_db.py [--old-db PATH] [--new-db PATH] [--skip-financials]

迁移内容:
  master_stock_info → stock_info（直接查，不做 JOIN 以避免丢行）
  daily_klines → daily_klines（含 pre_close/change_pct/turnover_rate 计算）
  stock_valuation → stock_valuation

跳过:
  stock_financials → 由 Fuyao API 重抓（旧表列映射质量差，8列全为0）

不迁移:
  factor_* / backtest_* / watch_pool* / action_records*
"""

import os
import sys
import sqlite3
import argparse
from datetime import datetime

BATCH_SIZE = 20000


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


def _ensure_new_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            industry TEXT DEFAULT '',
            market TEXT DEFAULT 'SZ',
            list_date TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL DEFAULT 0,
            high REAL DEFAULT 0,
            low REAL DEFAULT 0,
            close REAL DEFAULT 0,
            volume REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            pre_close REAL DEFAULT 0,
            change_pct REAL DEFAULT 0,
            turnover_rate REAL DEFAULT 0,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_valuation (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            pe_ttm REAL DEFAULT 0,
            pb_mrq REAL DEFAULT 0,
            ps_ttm REAL DEFAULT 0,
            market_cap REAL DEFAULT 0,
            total_shares REAL DEFAULT 0,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_financials (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            eps REAL DEFAULT 0,
            roe REAL DEFAULT 0,
            revenue REAL DEFAULT 0,
            profit REAL DEFAULT 0,
            total_assets REAL DEFAULT 0,
            total_liabilities REAL DEFAULT 0,
            net_assets REAL DEFAULT 0,
            operating_cash_flow REAL DEFAULT 0,
            gross_margin REAL DEFAULT 0,
            net_margin REAL DEFAULT 0,
            PRIMARY KEY (code, date)
        )
    """)
    conn.commit()


def migrate(args):
    old_db = args.old_db or get_old_db_path()
    if not old_db or not os.path.exists(old_db):
        print(f"错误: 旧数据库未找到 ({old_db})")
        print("请指定 --old-db 路径")
        sys.exit(1)

    new_db = args.new_db or "data/qs.db"
    os.makedirs(os.path.dirname(new_db) or ".", exist_ok=True)

    print(f"旧数据库: {old_db} ({os.path.getsize(old_db) / 1024**3:.1f} GB)")
    print(f"新数据库: {new_db}")
    print()

    old_conn = sqlite3.connect(old_db)
    new_conn = sqlite3.connect(new_db)

    # 确保目标表存在
    _ensure_new_tables(new_conn)

    old_conn.row_factory = sqlite3.Row
    old_tables = [
        r[0] for r in old_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    print(f"旧数据库表: {', '.join(old_tables)}")
    print()

    # [1/3] 迁移 master_stock_info → stock_info
    # 直接查 master_stock_info，不做 JOIN（避免丢行）
    # stocks_master 的 market/list_date 信息通过 stocks_master 补充
    if "master_stock_info" in old_tables:
        print("[1/3] 迁移股票信息...")
        # 先从 stocks_master 建一个 market/list_date 的查找表
        sm_lookup = {}
        if "stocks_master" in old_tables:
            for r in old_conn.execute("SELECT code, market, list_date FROM stocks_master").fetchall():
                sm_lookup[r["code"]] = (r["market"] or "SZ", r["list_date"] or "")

        rows = old_conn.execute("SELECT code, name, industry FROM master_stock_info").fetchall()
        new_conn.execute("DELETE FROM stock_info")
        insert_rows = []
        for r in rows:
            market, list_date = sm_lookup.get(r["code"], ("SZ", ""))
            insert_rows.append((r["code"], r["name"] or r["code"], r["industry"] or "", market, list_date))
        new_conn.executemany(
            "INSERT OR IGNORE INTO stock_info (code, name, industry, market, list_date, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            insert_rows,
        )
        new_conn.commit()
        print(f"  ✓ {len(insert_rows)} 条记录")

    # [2/3] 迁移 daily_klines（分批，12M+ 行）
    # 旧表没有 pre_close/change_pct/turnover_rate，写 0（管道后续会补全）
    # 旧表日期格式为 YYYYMMDD，需要转为 YYYY-MM-DD
    if "daily_klines" in old_tables:
        print("[2/3] 迁移 daily_klines (分批)...")
        total = 0
        offset = 0
        while True:
            rows = old_conn.execute("""
                SELECT code, date, open, high, low, close, volume, amount
                FROM daily_klines ORDER BY code, date LIMIT ? OFFSET ?
            """, (BATCH_SIZE, offset)).fetchall()
            if not rows:
                break
            insert_rows = []
            for r in rows:
                d = r["date"] or ""
                # YYYYMMDD → YYYY-MM-DD
                if len(d) == 8 and d.isdigit():
                    d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                insert_rows.append((
                    r["code"], d, r["open"] or 0, r["high"] or 0, r["low"] or 0,
                    r["close"] or 0, r["volume"] or 0, r["amount"] or 0,
                ))
            new_conn.executemany(
                "INSERT OR IGNORE INTO daily_klines (code, date, open, high, low, close, volume, amount, pre_close, change_pct, turnover_rate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0)",
                insert_rows,
            )
            new_conn.commit()
            total += len(rows)
            offset += BATCH_SIZE
            print(f"  → {total} 条...", end="\r")
        print(f"  ✓ {total} 条记录")

    # [3/3] 迁移 stock_valuation
    if "stock_valuation" in old_tables:
        print("[3/3] 迁移 stock_valuation...")
        rows = old_conn.execute("SELECT * FROM stock_valuation").fetchall()
        new_conn.execute("DELETE FROM stock_valuation")
        new_conn.executemany(
            "INSERT OR IGNORE INTO stock_valuation (code, date, pe_ttm, pb_mrq, ps_ttm, market_cap, total_shares) VALUES (?, ?, ?, ?, ?, 0, 0)",
            [(r["code"], r["date"] or "", r["pe_ttm"] or 0, r["pb_realty"] or 0, r["ps_ttm"] or 0) for r in rows],
        )
        new_conn.commit()
        print(f"  ✓ {len(rows)} 条记录")

    # stock_financials — 跳过迁移
    # 旧表只有 roe/gross_margin/net_margin 有效，其他 8 列全为 0
    # 由 Fuyao API 重抓完整财务数据（Phase 3.5）
    if "stock_financials" in old_tables:
        print("[跳过] stock_financials — 由 Fuyao API 重抓（旧数据质量差）")

    old_conn.close()
    new_conn.close()

    print()
    print("迁移完成!")
    print("  - stock_info: 从 master_stock_info 直接迁移")
    print("  - daily_klines: 含 pre_close/change_pct/turnover_rate (写0，管道后续补全)")
    print("  - stock_valuation: 从旧表迁移")
    print("  - stock_financials: 跳过，由 Fuyao API 重抓")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从旧 stocks_full.db 迁移数据")
    parser.add_argument("--old-db", help="旧数据库路径")
    parser.add_argument("--new-db", default="data/qs.db", help="新数据库路径")
    args = parser.parse_args()
    migrate(args)
#!/usr/bin/env python3
"""迁移额外数据表: stock_moneyflow, stock_news, stock_limit_up_down

用法: python scripts/migrate_additional_tables.py [--old-db PATH] [--new-db PATH]
"""

import os
import sys
import sqlite3
import argparse


def _ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_moneyflow (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            main_net_inflow REAL DEFAULT 0,
            retail_net_inflow REAL DEFAULT 0,
            main_inflow REAL DEFAULT 0,
            main_outflow REAL DEFAULT 0,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_news (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT DEFAULT '',
            sentiment TEXT DEFAULT 'neutral',
            sentiment_score REAL DEFAULT 0,
            PRIMARY KEY (code, date, title)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_limit_up_down (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            name TEXT DEFAULT '',
            limit_type TEXT NOT NULL,
            price REAL DEFAULT 0,
            change_pct REAL DEFAULT 0,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (code, date, limit_type)
        )
    """)
    conn.commit()


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


def migrate(args):
    old_db = args.old_db or get_old_db_path()
    new_db = args.new_db or "data/qs.db"
    os.makedirs(os.path.dirname(new_db) or ".", exist_ok=True)

    new_conn = sqlite3.connect(new_db)
    _ensure_tables(new_conn)
    print(f"[✓] 目标表已创建: {new_db}")

    if old_db and os.path.exists(old_db):
        old_conn = sqlite3.connect(old_db)
        old_conn.row_factory = sqlite3.Row
        old_tables = [
            r[0] for r in old_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        print(f"旧数据库表: {', '.join(old_tables)}")

        migrated = 0
        if "moneyflow_daily" in old_tables:
            rows = old_conn.execute("SELECT * FROM moneyflow_daily").fetchall()
            if rows:
                new_conn.execute("DELETE FROM stock_moneyflow")
                new_conn.executemany(
                    "INSERT OR IGNORE INTO stock_moneyflow (code, date, main_net_inflow, retail_net_inflow, main_inflow, main_outflow) VALUES (?, ?, ?, ?, ?, ?)",
                    [(r["code"], r["date"],
                      r["net_amount"] or 0,
                      (r["buy_amount_small"] or 0) + (r["buy_amount_medium"] or 0) - (r["sell_amount_small"] or 0) - (r["sell_amount_medium"] or 0),
                      (r["buy_amount_large"] or 0) + (r["buy_amount_huge"] or 0),
                      (r["sell_amount_large"] or 0) + (r["sell_amount_huge"] or 0),
                      ) for r in rows],
                )
                new_conn.commit()
                migrated += len(rows)
                print(f"  ✓ stock_moneyflow (from moneyflow_daily): {len(rows)} 条")

        if "limit_up_down" in old_tables:
            rows = old_conn.execute("SELECT * FROM limit_up_down").fetchall()
            if rows:
                new_conn.execute("DELETE FROM stock_limit_up_down")
                new_conn.executemany(
                    "INSERT OR IGNORE INTO stock_limit_up_down (code, date, name, limit_type, price, change_pct, count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(r["code"], r["date"], r["name"] or "",
                      r["type"],
                      r["price"] or 0, r["change_pct"] or 0,
                      r["limit_count"] or 0) for r in rows],
                )
                new_conn.commit()
                migrated += len(rows)
                print(f"  ✓ stock_limit_up_down (from limit_up_down): {len(rows)} 条")

        old_conn.close()
        if migrated > 0:
            print(f"\n共迁移 {migrated} 条记录")
        else:
            print("\n旧数据库中没有匹配表，已创建空表结构")
    else:
        print(f"旧数据库未找到 ({old_db})，已创建空表结构")

    new_conn.close()
    print("\n迁移完成!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="迁移额外数据表")
    parser.add_argument("--old-db", help="旧数据库路径")
    parser.add_argument("--new-db", default="data/qs.db", help="新数据库路径")
    args = parser.parse_args()
    migrate(args)
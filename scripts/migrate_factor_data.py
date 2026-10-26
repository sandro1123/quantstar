#!/usr/bin/env python3
"""迁移因子数据表: factor_daily, alpha_factors, model_predictions, stock_financials, factor_ic, factor_weights, stock_valuation(dividend_yield)

用法:
  python scripts/migrate_factor_data.py                        # 正式迁移
  python scripts/migrate_factor_data.py --dry-run              # 预览
  python scripts/migrate_factor_data.py --skip factor_daily    # 跳过指定表
"""

import os
import sys
import sqlite3
import argparse
from datetime import datetime

BATCH_SIZE = 20000

TABLE_CONFIG = {
    "factor_daily": {
        "desc": "因子日数据 (690万行, 16.5年)",
        "old_table": "factor_daily",
        "needs_create": True,
        "needs_date_convert": True,
        "date_column": "date",
    },
    "alpha_factors": {
        "desc": "Alpha因子 (33万行, 3个月)",
        "old_table": "alpha_factors",
        "needs_create": True,
        "needs_date_convert": True,
        "date_column": "date",
    },
    "model_predictions": {
        "desc": "模型预测 (29万行, 3个月)",
        "old_table": "model_predictions",
        "needs_create": True,
        "needs_date_convert": True,
        "date_column": "date",
    },
    "stock_financials": {
        "desc": "财务数据追加 (1.6万行, 22年)",
        "old_table": "stock_financials",
        "needs_create": False,
        "needs_map": True,
    },
    "factor_ic": {
        "desc": "因子IC (3.9万行, 16.5年)",
        "old_table": "factor_ic",
        "needs_create": True,
        "needs_date_convert": True,
        "date_column": "calc_date",
    },
    "factor_weights": {
        "desc": "因子权重 (53行, 1个月)",
        "old_table": "factor_weights",
        "needs_create": True,
        "needs_date_convert": False,
    },
    "stock_valuation_dividend": {
        "desc": "stock_valuation 追加 dividend_yield (5966行)",
        "old_table": "stock_valuation",
        "needs_create": False,
        "needs_date_convert": False,
        "needs_column_add": True,
    },
    "stock_sentiment": {
        "desc": "新闻情绪 (35462行, 2个月)",
        "old_table": "stock_sentiment",
        "needs_create": True,
        "needs_date_convert": True,
        "date_column": "date",
    },
    "concept_daily": {
        "desc": "概念板块归属 (684376行, 6个月)",
        "old_table": "concept_daily",
        "needs_create": True,
        "needs_date_convert": True,
        "date_column": "date",
    },
    "analyst_forecasts": {
        "desc": "分析师盈利预测 (9317行)",
        "old_table": "analyst_forecasts",
        "needs_create": True,
        "needs_date_convert": True,
        "date_column": "date",
    },
    "lhb_daily": {
        "desc": "龙虎榜数据 (2051行, 1个月)",
        "old_table": "lhb_daily",
        "needs_create": True,
        "needs_date_convert": True,
        "date_column": "date",
    },
}


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


def _convert_date(d: str) -> str:
    if d and len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def _get_columns(conn, table: str) -> list:
    c = conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in c.fetchall()]


def _get_cols_sql(conn, table: str) -> str:
    cols = _get_columns(conn, table)
    return ", ".join(cols)


def _get_placeholders(count: int) -> str:
    return ", ".join(["?"] * count)


def _ensure_table_factor_daily(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factor_daily (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            pe_ttm REAL, pb REAL, ps_ttm REAL,
            roe REAL, gross_margin REAL, net_margin REAL,
            rev_growth REAL, profit_growth REAL,
            momentum_5d REAL, momentum_20d REAL,
            volatility_20d REAL, volume_ratio REAL,
            macd_hist REAL, rsi_14 REAL, ma_status TEXT,
            fwd_return_1d REAL, fwd_return_5d REAL, fwd_return_20d REAL,
            pe_rank REAL, pb_rank REAL, roe_rank REAL,
            momentum_rank REAL, macd_rank REAL,
            analyst_consensus_score REAL, lhb_drama_score REAL,
            lhb_institution_score REAL, xueqiu_heat_score REAL,
            total_mv REAL, circ_mv REAL, concept_count INTEGER,
            rsi6 REAL, rsi12 REAL, turnover_rate REAL,
            momentum_score REAL, change_pct REAL,
            PRIMARY KEY (date, code)
        )
    """)


def _ensure_table_alpha_factors(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alpha_factors (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            kmid REAL, klen REAL, kmid2 REAL, kup REAL, kup2 REAL,
            klow REAL, klow2 REAL, ksft REAL, ksft2 REAL,
            roc5 REAL, roc10 REAL, roc20 REAL, roc60 REAL,
            ma5_dev REAL, ma10_dev REAL,
            std5 REAL, std10 REAL, std20 REAL,
            max5 REAL, min5 REAL, rank5 REAL, rank10 REAL,
            beta10 REAL, rsqr10 REAL,
            corr5 REAL, corr10 REAL, corr20 REAL,
            cord5 REAL, cord10 REAL, cord20 REAL,
            cntp5 REAL, cntp10 REAL, cntp20 REAL,
            cntd5 REAL, cntd10 REAL, cntd20 REAL,
            sump5 REAL, sump10 REAL, sump20 REAL,
            sumd5 REAL, sumd10 REAL, sumd20 REAL,
            vma5 REAL, vma10 REAL, vma20 REAL,
            vstd5 REAL, vstd10 REAL, wvma5 REAL,
            vsump5 REAL, vsumn5 REAL, vsumd5 REAL,
            rsi14 REAL, macd_hist REAL, bb_width REAL,
            aroon_up REAL, aroon_down REAL,
            alpha002 REAL, alpha006 REAL, alpha010 REAL, alpha101 REAL,
            z_kmid REAL, z_klen REAL, z_kmid2 REAL, z_kup REAL, z_kup2 REAL,
            z_klow REAL, z_klow2 REAL, z_ksft REAL, z_ksft2 REAL,
            z_roc5 REAL, z_roc10 REAL, z_roc20 REAL, z_roc60 REAL,
            z_ma5_dev REAL, z_ma10_dev REAL,
            z_std5 REAL, z_std10 REAL, z_std20 REAL,
            z_max5 REAL, z_min5 REAL, z_rank5 REAL, z_rank10 REAL,
            z_beta10 REAL, z_rsqr10 REAL,
            z_corr5 REAL, z_corr10 REAL, z_corr20 REAL,
            z_cord5 REAL, z_cord10 REAL, z_cord20 REAL,
            z_cntp5 REAL, z_cntp10 REAL, z_cntp20 REAL,
            z_cntd5 REAL, z_cntd10 REAL, z_cntd20 REAL,
            z_sump5 REAL, z_sump10 REAL, z_sump20 REAL,
            z_sumd5 REAL, z_sumd10 REAL, z_sumd20 REAL,
            z_vma5 REAL, z_vma10 REAL, z_vma20 REAL,
            z_vstd5 REAL, z_vstd10 REAL, z_wvma5 REAL,
            z_vsump5 REAL, z_vsumn5 REAL, z_vsumd5 REAL,
            z_rsi14 REAL, z_macd_hist REAL, z_bb_width REAL,
            z_aroon_up REAL, z_aroon_down REAL,
            z_alpha002 REAL, z_alpha006 REAL, z_alpha010 REAL, z_alpha101 REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (date, code)
        )
    """)


def _ensure_table_model_predictions(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_predictions (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            ml_score REAL,
            factor_score REAL,
            blended_score REAL,
            actual_return_5d REAL,
            actual_return_10d REAL,
            actual_return_20d REAL,
            model_version TEXT DEFAULT 'lgb_latest',
            PRIMARY KEY (date, code)
        )
    """)


def _ensure_table_factor_ic(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factor_ic (
            calc_date TEXT NOT NULL,
            factor_name TEXT NOT NULL,
            mean_ic REAL,
            std_ic REAL,
            icir REAL,
            win_rate REAL,
            effectiveness TEXT,
            sample_size INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (calc_date, factor_name)
        )
    """)


def _ensure_table_factor_weights(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factor_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            weight_date TEXT NOT NULL,
            factor_name TEXT NOT NULL,
            weight REAL NOT NULL,
            method TEXT,
            old_weight REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(weight_date, factor_name)
        )
    """)


def _ensure_tables(conn):
    _ensure_table_factor_daily(conn)
    _ensure_table_alpha_factors(conn)
    _ensure_table_model_predictions(conn)
    _ensure_table_factor_ic(conn)
    _ensure_table_factor_weights(conn)
    _ensure_table_stock_sentiment(conn)
    _ensure_table_concept_daily(conn)
    _ensure_table_analyst_forecasts(conn)
    _ensure_table_lhb_daily(conn)
    conn.commit()


def _ensure_table_stock_sentiment(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_sentiment (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            sentiment_score REAL,
            news_count INTEGER,
            positive_count INTEGER,
            negative_count INTEGER,
            PRIMARY KEY (date, code)
        )
    """)


def _ensure_table_concept_daily(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS concept_daily (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            concept_name TEXT,
            concept_change_pct REAL,
            concept_rank INTEGER,
            PRIMARY KEY (date, code, concept_name)
        )
    """)


def _ensure_table_analyst_forecasts(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyst_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            forecast_type TEXT,
            target_price REAL,
            rating TEXT,
            analyst_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _ensure_table_lhb_daily(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lhb_daily (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            stock_name TEXT,
            close_price REAL,
            change_pct REAL,
            net_buy REAL,
            buy_amount REAL,
            sell_amount REAL,
            reason TEXT,
            PRIMARY KEY (date, code)
        )
    """)


def _has_data(conn, table: str) -> bool:
    try:
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
        return cursor.fetchone()[0] > 0
    except Exception:
        return False


def _add_column_if_not_exists(conn, table: str, col: str, col_type: str):
    cursor = conn.execute(f"PRAGMA table_info({table})")
    existing = {r[1] for r in cursor.fetchall()}
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        return True
    return False


def _migrate_batch_with_date(
    old_conn, new_conn, old_table: str, new_table: str,
    old_cols: list, new_cols: list, date_col: str,
    dry_run: bool = False,
) -> int:
    if dry_run:
        count = old_conn.execute(f"SELECT COUNT(*) FROM {old_table}").fetchone()[0]
        return count

    total = 0
    offset = 0
    old_cols_list = ", ".join(old_cols)
    placeholders = _get_placeholders(len(new_cols))
    insert_sql = f"INSERT OR IGNORE INTO {new_table} ({', '.join(new_cols)}) VALUES ({placeholders})"

    date_idx = old_cols.index(date_col)

    # 判断是否有 code 列用于排序
    has_code = "code" in old_cols
    order_by = f"{date_col}, code" if has_code else date_col

    while True:
        rows = old_conn.execute(
            f"SELECT {old_cols_list} FROM {old_table} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (BATCH_SIZE, offset),
        ).fetchall()
        if not rows:
            break

        insert_rows = []
        for r in rows:
            row_list = list(r)
            row_list[date_idx] = _convert_date(str(row_list[date_idx] or ""))
            insert_rows.append(tuple(row_list))

        if not dry_run:
            new_conn.executemany(insert_sql, insert_rows)
            new_conn.commit()

        total += len(rows)
        offset += BATCH_SIZE
        print(f"  → {total} 条...", end="\r")

    return total


def _migrate_batch_direct(
    old_conn, new_conn, old_table: str, new_table: str,
    old_cols: list, new_cols: list,
    dry_run: bool = False,
) -> int:
    if dry_run:
        count = old_conn.execute(f"SELECT COUNT(*) FROM {old_table}").fetchone()[0]
        return count

    total = 0
    offset = 0
    old_cols_list = ", ".join(old_cols)
    placeholders = _get_placeholders(len(new_cols))
    insert_sql = f"INSERT OR IGNORE INTO {new_table} ({', '.join(new_cols)}) VALUES ({placeholders})"

    order_cols = [c for c in old_cols if c in ("date", "code", "calc_date", "weight_date")]
    order_by = ", ".join(order_cols) if order_cols else old_cols[0]

    while True:
        rows = old_conn.execute(
            f"SELECT {old_cols_list} FROM {old_table} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (BATCH_SIZE, offset),
        ).fetchall()
        if not rows:
            break

        if not dry_run:
            new_conn.executemany(insert_sql, rows)
            new_conn.commit()

        total += len(rows)
        offset += BATCH_SIZE
        print(f"  → {total} 条...", end="\r")

    return total


def _migrate_financials(old_conn, new_conn, dry_run: bool = False) -> int:
    if dry_run:
        count = old_conn.execute("SELECT COUNT(*) FROM stock_financials").fetchone()[0]
        return count

    old_rows = old_conn.execute(
        "SELECT code, report_date, roe, gross_margin, net_margin, "
        "asset_liability_ratio, revenue_growth, profit_growth "
        "FROM stock_financials ORDER BY code, report_date"
    ).fetchall()

    insert_rows = []
    for r in old_rows:
        code, report_date, roe, gross_margin, net_margin = r[0], r[1], r[2], r[3], r[4]
        asset_liability_ratio = r[5] or 0
        revenue_growth = r[6] or 0
        profit_growth = r[7] or 0

        date_val = _convert_date(str(report_date or ""))
        total_assets = 100.0
        total_liabilities = asset_liability_ratio

        insert_rows.append((
            code, date_val, 0, roe or 0, revenue_growth,
            profit_growth, total_assets, total_liabilities,
            0, 0, gross_margin or 0, net_margin or 0,
        ))

    if not dry_run:
        new_conn.executemany(
            "INSERT OR IGNORE INTO stock_financials "
            "(code, date, eps, roe, revenue, profit, total_assets, "
            "total_liabilities, net_assets, operating_cash_flow, "
            "gross_margin, net_margin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            insert_rows,
        )
        new_conn.commit()

    return len(insert_rows)


def _migrate_valuation_dividend(old_conn, new_conn, dry_run: bool = False) -> int:
    if dry_run:
        count = old_conn.execute(
            "SELECT COUNT(*) FROM stock_valuation WHERE dividend_yield IS NOT NULL"
        ).fetchone()[0]
        return count

    old_rows = old_conn.execute(
        "SELECT code, date, dividend_yield FROM stock_valuation "
        "WHERE dividend_yield IS NOT NULL ORDER BY code, date"
    ).fetchall()

    insert_rows = []
    for r in old_rows:
        code = r[0]
        date_val = _convert_date(str(r[1] or ""))
        div_yield = r[2] or 0
        insert_rows.append((code, date_val, div_yield))

    if not dry_run:
        new_conn.executemany(
            "UPDATE stock_valuation SET dividend_yield = ? "
            "WHERE code = ? AND date = ? AND dividend_yield IS NULL",
            [(dy, c, d) for c, d, dy in insert_rows],
        )
        new_conn.commit()

        new_inserts = []
        for r in insert_rows:
            new_conn.execute(
                "INSERT OR IGNORE INTO stock_valuation "
                "(code, date, pe_ttm, pb_mrq, ps_ttm, market_cap, total_shares, dividend_yield) "
                "VALUES (?, ?, 0, 0, 0, 0, 0, ?)",
                (r[0], r[1], r[2]),
            )
        new_conn.commit()

    return len(insert_rows)


def migrate(args):
    old_db = args.old_db or get_old_db_path()
    if not old_db or not os.path.exists(old_db):
        print(f"错误: 旧数据库未找到 ({old_db})")
        print("请指定 --old-db 路径")
        sys.exit(1)

    new_db = args.new_db or "data/qs.db"
    os.makedirs(os.path.dirname(new_db) or ".", exist_ok=True)

    dry_run = args.dry_run
    skip_tables = set(args.skip or [])

    print(f"旧数据库: {old_db} ({os.path.getsize(old_db) / 1024**3:.1f} GB)")
    print(f"新数据库: {new_db}")
    if dry_run:
        print("模式: 预览 (--dry-run, 不写入)")
    print()

    old_conn = sqlite3.connect(old_db)
    new_conn = sqlite3.connect(new_db)
    old_conn.row_factory = sqlite3.Row

    old_tables = [
        r[0] for r in old_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    old_tables_set = set(old_tables)
    print(f"旧数据库表: {', '.join(old_tables)}")
    print()

    if not dry_run:
        _ensure_tables(new_conn)

    if not dry_run:
        if "stock_valuation" in [
            r[0] for r in new_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_valuation'"
            ).fetchall()
        ]:
            added = _add_column_if_not_exists(new_conn, "stock_valuation", "dividend_yield", "REAL")
            if added:
                print("  → 新增 stock_valuation.dividend_yield 列")
            new_conn.commit()

    results = {}

    for table_key, config in TABLE_CONFIG.items():
        old_table = config["old_table"]
        desc = config["desc"]

        if table_key in skip_tables:
            print(f"[跳过] {table_key} ({desc}) — 用户指定跳过")
            results[table_key] = "skipped"
            continue

        if old_table not in old_tables_set:
            print(f"[跳过] {table_key} ({desc}) — 旧表不存在")
            results[table_key] = "skipped"
            continue

        if config.get("needs_column_add"):
            pass
        elif not config.get("needs_create"):
            pass
        else:
            new_table = table_key
            if _has_data(new_conn, new_table) and not args.force:
                print(f"[跳过] {table_key} ({desc}) — 目标表已有数据")
                results[table_key] = "skipped"
                continue

        if config.get("needs_map"):
            print(f"[迁移] {table_key} ({desc}) — 字段映射")
            count = _migrate_financials(old_conn, new_conn, dry_run=dry_run)
            print(f"  {'✓' if not dry_run else '→'} {count} 条记录")
            results[table_key] = count
            continue

        if config.get("needs_column_add"):
            print(f"[迁移] {table_key} ({desc}) — 追加 dividend_yield")
            count = _migrate_valuation_dividend(old_conn, new_conn, dry_run=dry_run)
            print(f"  {'✓' if not dry_run else '→'} {count} 条记录")
            results[table_key] = count
            continue

        new_table = table_key
        old_cols = _get_columns(old_conn, old_table)

        obsolete_cols = {"id", "created_at", "updated_at", "update_time"}
        old_cols = [c for c in old_cols if c not in obsolete_cols]

        new_cols = old_cols[:]
        date_col = config.get("date_column")

        if config.get("needs_date_convert") and date_col:
            print(f"[迁移] {table_key} ({desc}) — 分批处理, 日期转换")
            count = _migrate_batch_with_date(
                old_conn, new_conn, old_table, new_table,
                old_cols, new_cols, date_col,
                dry_run=dry_run,
            )
        else:
            print(f"[迁移] {table_key} ({desc})")
            count = _migrate_batch_direct(
                old_conn, new_conn, old_table, new_table,
                old_cols, new_cols,
                dry_run=dry_run,
            )

        print(f"  {'✓' if not dry_run else '→'} {count} 条记录")
        results[table_key] = count

    old_conn.close()
    new_conn.close()

    print()
    print("=" * 60)
    if dry_run:
        print("迁移预览 (未执行写入):")
    else:
        print("迁移完成:")
    print("=" * 60)
    for key, count in results.items():
        desc = TABLE_CONFIG[key]["desc"]
        if count == "skipped":
            print(f"  {key:35s} 跳过")
        else:
            print(f"  {key:35s} {count:>8} 条")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="迁移因子数据表")
    parser.add_argument("--old-db", help="旧数据库路径")
    parser.add_argument("--new-db", default="data/qs.db", help="新数据库路径")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不写入数据")
    parser.add_argument("--force", action="store_true", help="强制迁移（即使目标表已有数据）")
    parser.add_argument("--skip", nargs="*", help="跳过指定表")
    parser.add_argument("--add-dividend-yield", action="store_true", help="仅添加 dividend_yield 列")

    args = parser.parse_args()

    if args.add_dividend_yield:
        args.dry_run = True

    migrate(args)
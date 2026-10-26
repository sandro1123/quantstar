# -*- coding: utf-8 -*-
"""W2 卡① (2026-09-20): freshness_audit 对 T+1 源改审「上一交易日」。

病根 (alert#115 同族假阳): index_daily/lhb_daily 是 T+1 源 (akshare_sina
次日 ~08:03 落库), freshness_audit 16:22 跑时当日行必然未落,
check_collect_freshness 查 WHERE date = 当日 → 恒 missing → 每日假告警。

修法: readiness 新增 T1_COLLECT_TABLES 注册表 (T+1 源), 这类表查
「上一交易日」而非当日; 上一交易日用 daily_klines 的 MAX(date)<date 映射
(真实交易日历, 非自然日 — 周五审计周一数据会错 3 天)。

三态 (参照 census_guard 判例: 空集显式报警):
- 上一交易日有行 → ok (源履约)
- 上一交易日无行 → missing (源该发未发 = 真红)
- 无更早交易日行 (表未首跑) → missing (哨兵不能哑)

验收锚: 周一 16:37 freshness 对 index_daily 不再报 missing;
T+1 源昨日也缺行的用例必须真红。
"""
import sqlite3

import pytest

from common.database import Database


def _db():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    # T+1 源表 + 交易日历锚表
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS index_daily (
            code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS lhb_daily (
            code TEXT, date TEXT, net_amount REAL, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
    """)
    conn.commit()
    return db, conn


def _seed_klines(conn, dates):
    for d in dates:
        conn.execute(
            "INSERT OR REPLACE INTO daily_klines (code, date, close) VALUES ('600519', ?, 1500.0)",
            (d,))
    conn.commit()


# ---- 注册表存在且名单正确 ----

def test_t1_registry_exists():
    """T1_COLLECT_TABLES 注册表必须存在且含 index_daily + lhb_daily。"""
    from data_layer.health.readiness import DataReadiness
    t1 = getattr(DataReadiness, "T1_COLLECT_TABLES", None)
    assert t1 is not None, "缺 T1_COLLECT_TABLES 注册表 (T+1 源无载体)"
    assert "index_daily" in t1, "index_daily 是 T+1 源必须入册"
    assert "lhb_daily" in t1, "lhb_daily 是 T+1 源必须入册"
    # 注册表格式: {table: date_col} 与 COLLECT_TABLES 同构
    assert t1["index_daily"] == "date"
    assert t1["lhb_daily"] == "date"


def test_t1_not_in_same_day_registry():
    """T+1 源不得同时留在当日清单 (否则双重判定, 当日那条仍假阳)。"""
    from data_layer.health.readiness import DataReadiness
    for t in ("index_daily", "lhb_daily"):
        assert t not in DataReadiness.COLLECT_TABLES, \
            f"{t} 是 T+1 源, 留在当日清单必然每日假阳"


# ---- 核心语义: 查上一交易日 ----

def test_t1_source_fresh_when_prev_trading_day_has_rows():
    """周一审计: 当日(周一)无行但上一交易日(周五)有行 → fresh=True。

    这是 #115 假阳的反面: 周五 16:22 审计时 index_daily 已有周五行
    (akshare_sina 08:03 落的), 当日无行是 T+1 源的天然时序, 不是断供。
    """
    from data_layer.health.readiness import DataReadiness
    db, conn = _db()
    _seed_klines(conn, ["2026-09-17", "2026-09-18"])  # 周三/周四 = 交易日
    # index_daily 只落到 09-17 (T+1 源, 09-18 的行要 09-19 08:03 才落)
    conn.execute("INSERT INTO index_daily (code, date, close) VALUES ('000300.SH', '2026-09-17', 4000.0)")
    conn.commit()
    r = DataReadiness(db).check_collect_freshness("2026-09-18")
    assert r["index_daily"]["fresh"] is True, \
        "T+1 源上一交易日(09-17)有行必须判 fresh — 否则 #115 假阳未修"


def test_t1_source_missing_when_prev_trading_day_also_empty():
    """T+1 源上一交易日也无行 → fresh=False (真红: 源该发未发)。"""
    from data_layer.health.readiness import DataReadiness
    db, conn = _db()
    _seed_klines(conn, ["2026-09-17", "2026-09-18"])
    # index_daily 停在 09-16 (09-17 无行 = 源断供一天)
    conn.execute("INSERT INTO index_daily (code, date, close) VALUES ('000300.SH', '2026-09-16', 3990.0)")
    conn.commit()
    r = DataReadiness(db).check_collect_freshness("2026-09-18")
    assert r["index_daily"]["fresh"] is False, \
        "上一交易日(09-17)无行必须判 missing — 这才是真断供"


def test_prev_trading_day_uses_klines_calendar_not_natural():
    """上一交易日映射必须走 daily_klines (真实交易日历), 不能是 date-1 自然日。

    周一 09-21 的上一交易日是周五 09-18 (隔 3 天), 用自然日会查周日 09-20
    恒无行 → 每周一假红。
    """
    from data_layer.health.readiness import DataReadiness
    db, conn = _db()
    # 最近交易日 = 周五 09-18 (审计日 = 周一 09-21)
    _seed_klines(conn, ["2026-09-17", "2026-09-18"])
    conn.execute("INSERT INTO index_daily (code, date, close) VALUES ('000300.SH', '2026-09-18', 4010.0)")
    conn.commit()
    r = DataReadiness(db).check_collect_freshness("2026-09-21")  # 周一
    assert r["index_daily"]["fresh"] is True, \
        "周一审计必须查周五(09-18)而非周日(09-20) — 自然日映射每周一假红"


def test_t1_source_no_history_at_all_is_missing():
    """T+1 源连更早交易日都无行 (表未首跑/从未落数) → missing (哨兵不哑)。"""
    from data_layer.health.readiness import DataReadiness
    db, conn = _db()
    _seed_klines(conn, ["2026-09-17", "2026-09-18"])
    # index_daily 全空
    r = DataReadiness(db).check_collect_freshness("2026-09-18")
    assert r["index_daily"]["fresh"] is False, \
        "零历史必须 missing — 空集显式报警 (census_guard 判例)"


def test_same_day_tables_still_check_same_day():
    """非 T+1 源 (如 lhb 之外的冷采集表) 仍查当日 — 修法不扩大范围。"""
    from data_layer.health.readiness import DataReadiness
    db, conn = _db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS limit_up_ladder (
            code TEXT, snapshot_date TEXT, PRIMARY KEY(code, snapshot_date));
    """)
    # 上一交易日有行, 当日无行
    conn.execute("INSERT INTO limit_up_ladder (code, snapshot_date) VALUES ('600519', '2026-09-17')")
    conn.commit()
    r = DataReadiness(db).check_collect_freshness("2026-09-18")
    assert r["limit_up_ladder"]["fresh"] is False, \
        "非 T+1 源仍查当日 — 当日无行=missing 的旧语义不能动"

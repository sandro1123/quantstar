# -*- coding: utf-8 -*-
"""W2-1 丙案 (2026-09-21): kline unknown_gap 显形 + 双指标。

审计裁定 (sm-e6042f95f7004c67): 停牌/陈旧码不做「排除」只做「显形」。
- 60 天无 K线 且 is_active=1 → classification='unknown' 写显形表, 不声称停牌、
  不从分母移除、不静默吞 errors (未知缺失必须显形)。
- 活跃股 (60 天内有 K线) 空返回 → 仍进 errors 判源故障 (不误伤)。
- 显形表 pipeline_kline_exceptions (date, code, reason) 主键,
  留 last_kline_date + source_attempts。
"""
import sqlite3

import pytest

from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL


def _db():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_klines (
        code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date))""")
    conn.commit()
    return db, conn


def _seed(conn, code, dates):
    for d in dates:
        conn.execute("INSERT OR REPLACE INTO daily_klines (code, date, close) VALUES (?,?,?)",
                     (code, d, 10.0))
    conn.commit()


# ---- 显形表写入 ----

def test_unknown_gap_recorded_in_exception_table():
    """60 天无 K线 → 写 pipeline_kline_exceptions, classification='unknown'。"""
    from data_layer.pipelines.kline import _record_unknown_gap, _UNKNOWN_GAP_DDL
    db, conn = _db()
    conn.executescript(_UNKNOWN_GAP_DDL)
    conn.commit()
    _record_unknown_gap(conn, "2026-09-21", "920305", "2026-07-28", "all 4 sources empty")
    row = conn.execute(
        "SELECT code, reason, classification, last_kline_date FROM pipeline_kline_exceptions "
        "WHERE code='920305'").fetchone()
    assert row is not None, "unknown_gap 未写入显形表"
    assert row[1] == "no_kline_60d"
    assert row[2] == "unknown", "classification 必须是 unknown (不声称停牌)"
    assert row[3] == "2026-07-28"


def test_unknown_gap_classification_never_suspended():
    """显形表 classification 不得为 suspended/delisted (措辞红线)。"""
    from data_layer.pipelines.kline import _record_unknown_gap, _UNKNOWN_GAP_DDL
    db, conn = _db()
    conn.executescript(_UNKNOWN_GAP_DDL)
    conn.commit()
    _record_unknown_gap(conn, "2026-09-21", "000004", "2026-07-17", "all 4 sources empty")
    row = conn.execute(
        "SELECT classification FROM pipeline_kline_exceptions WHERE code='000004'").fetchone()
    assert row[0] not in ("suspended", "delisted", "halted"), \
        f"classification={row[0]} 违反「不可用未采到推停牌」"


def test_unknown_gap_idempotent_same_day():
    """同日同码重复写 → INSERT OR REPLACE 幂等, 不堆行。"""
    from data_layer.pipelines.kline import _record_unknown_gap, _UNKNOWN_GAP_DDL
    db, conn = _db()
    conn.executescript(_UNKNOWN_GAP_DDL)
    conn.commit()
    for _ in range(3):
        _record_unknown_gap(conn, "2026-09-21", "920305", "2026-07-28", "err")
    n = conn.execute(
        "SELECT COUNT(*) FROM pipeline_kline_exceptions WHERE code='920305'").fetchone()[0]
    assert n == 1, f"幂等失败, 堆了 {n} 行"


# ---- 60 天判据 (分组非断言) ----

def test_60d_no_kline_is_unknown_gap():
    """60 天无 K线 → _fetch_is_expected_empty=True (分组为 unknown_gap)。"""
    from data_layer.pipelines.kline import _fetch_is_expected_empty
    db, conn = _db()
    _seed(conn, "920305", ["2026-06-01"])  # 停 112 天
    assert _fetch_is_expected_empty(conn, "920305", "2026-09-21") is True


def test_active_stock_empty_is_source_failure():
    """活跃股 (近 60 天有 K线) 空返回 → False = 源故障, 必须进 errors。"""
    from data_layer.pipelines.kline import _fetch_is_expected_empty
    db, conn = _db()
    _seed(conn, "600519", ["2026-09-18", "2026-09-19"])
    assert _fetch_is_expected_empty(conn, "600519", "2026-09-21") is False


def test_last_kline_date_helper():
    """_last_kline_date 返回最近 K线日期, 无则 None。"""
    from data_layer.pipelines.kline import _last_kline_date
    db, conn = _db()
    _seed(conn, "920305", ["2026-07-27", "2026-07-28"])
    assert _last_kline_date(conn, "920305") == "2026-07-28"
    assert _last_kline_date(conn, "999999") is None

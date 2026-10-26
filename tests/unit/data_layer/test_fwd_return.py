"""DL-24: 跨层评估数据源基建 — forward_return 语义 + 截面读 API + 按日回填

:memory: 库, 无生产写。验证:
- forward_return: 未来未兑现 → None (不再假 0); 兑现 → 百分点; 优先 factor_daily
- get_fwd_returns 截面只含已兑现值; 表缺失 → {}
- backfill_fwd_returns: 仅写 NULL 行 / 幂等 / 未兑现保持 NULL
"""

import sqlite3

import pytest


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _mk_db(with_factor=True):
    db = _FakeDB()
    db.conn.execute(
        "CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, "
        "PRIMARY KEY (code, date))")
    if with_factor:
        db.conn.execute(
            "CREATE TABLE factor_daily (date TEXT, code TEXT, "
            "fwd_return_1d REAL, fwd_return_5d REAL, fwd_return_20d REAL, "
            "PRIMARY KEY (date, code))")
    db.conn.commit()
    return db


def _add_kline(db, code, date, close):
    db.conn.execute(
        "INSERT INTO daily_klines VALUES (?, ?, ?)", (code, date, close))
    db.conn.commit()


def _loader(db):
    from data_layer._loader import DataLoader
    return DataLoader(db)


# ---------- forward_return 语义 ----------

def test_forward_return_none_when_future_not_realized():
    """最新日期无后续交易日 → None, 不造假 0 (IC 环全 0 根因修复)"""
    db = _mk_db()
    _add_kline(db, "000001", "2026-08-14", 10.0)
    _add_kline(db, "000001", "2026-08-17", 10.5)  # 仅 1 个后续交易日
    ld = _loader(db)
    assert ld.forward_return("000001", "2026-08-17", horizon=5) is None
    # 08-14 后只有 1 个交易日, 不足 5 → None
    assert ld.forward_return("000001", "2026-08-14", horizon=5) is None


def test_forward_return_percent_when_realized():
    """兑现时返回百分点 (与 factor_daily 存储单位一致)"""
    db = _mk_db()
    dates = ["2026-08-03", "2026-08-04", "2026-08-05",
             "2026-08-06", "2026-08-07", "2026-08-10"]
    for i, d in enumerate(dates):
        _add_kline(db, "000001", d, 10.0 + i)
    ld = _loader(db)
    # 08-03 后第 5 个交易日 = 08-10, close 15 → +50%
    ret = ld.forward_return("000001", "2026-08-03", horizon=5)
    assert ret == pytest.approx(50.0)


def test_forward_return_prefers_factor_daily():
    db = _mk_db()
    _add_kline(db, "000001", "2026-08-03", 10.0)
    db.conn.execute(
        "INSERT INTO factor_daily (date, code, fwd_return_5d) "
        "VALUES ('2026-08-03', '000001', 3.14)")
    db.conn.commit()
    ld = _loader(db)
    assert ld.forward_return("000001", "2026-08-03", horizon=5) == 3.14


def test_forward_return_falls_back_when_factor_table_missing():
    db = _mk_db(with_factor=False)
    dates = ["2026-08-03", "2026-08-04", "2026-08-05",
             "2026-08-06", "2026-08-07", "2026-08-10"]
    for i, d in enumerate(dates):
        _add_kline(db, "000001", d, 10.0 + i)
    ld = _loader(db)
    # factor_daily 不存在不崩, 落到 kline 现算: 08-03 后第 5 交易日 08-10 close 15 → +50%
    ret = ld.forward_return("000001", "2026-08-03", horizon=5)
    assert ret == pytest.approx(50.0)


# ---------- 截面读 API ----------

def test_get_fwd_returns_cross_section():
    db = _mk_db()
    db.conn.execute(
        "INSERT INTO factor_daily (date, code, fwd_return_5d) VALUES "
        "('2026-08-10', '000001', 1.5), ('2026-08-10', '000002', NULL), "
        "('2026-08-11', '000001', 9.9)")
    db.conn.commit()
    ld = _loader(db)
    cs = ld.fwd_returns_cross_section("2026-08-10", horizon=5)
    assert cs == {"000001": 1.5}  # NULL 不含, 他日不含


def test_get_fwd_returns_empty_when_table_missing():
    db = _mk_db(with_factor=False)
    ld = _loader(db)
    assert ld.fwd_returns_cross_section("2026-08-10", horizon=5) == {}


# ---------- 按日回填 ----------

def test_backfill_writes_only_null_rows():
    db = _mk_db()
    dates = ["2026-08-03", "2026-08-04", "2026-08-05",
             "2026-08-06", "2026-08-07", "2026-08-10"]
    for i, d in enumerate(dates):
        _add_kline(db, "000001", d, 10.0 + i)
    db.conn.execute(
        "INSERT INTO factor_daily (date, code, fwd_return_5d) "
        "VALUES ('2026-08-03', '000001', NULL)")
    db.conn.commit()
    ld = _loader(db)
    filled = ld.backfill_fwd_returns("2026-08-03", horizons=(5,))
    assert filled[5] == 1
    row = db.conn.execute(
        "SELECT fwd_return_5d FROM factor_daily "
        "WHERE date='2026-08-03' AND code='000001'").fetchone()
    assert row["fwd_return_5d"] == pytest.approx(50.0)


def test_backfill_idempotent_and_keeps_existing():
    db = _mk_db()
    dates = ["2026-08-03", "2026-08-04", "2026-08-05",
             "2026-08-06", "2026-08-07", "2026-08-10"]
    for i, d in enumerate(dates):
        _add_kline(db, "000001", d, 10.0 + i)
    db.conn.execute(
        "INSERT INTO factor_daily (date, code, fwd_return_5d) "
        "VALUES ('2026-08-03', '000001', 7.77)")  # 已有值
    db.conn.commit()
    ld = _loader(db)
    filled = ld.backfill_fwd_returns("2026-08-03", horizons=(5,))
    assert filled[5] == 0  # 不覆盖已有值
    row = db.conn.execute(
        "SELECT fwd_return_5d FROM factor_daily "
        "WHERE date='2026-08-03' AND code='000001'").fetchone()
    assert row["fwd_return_5d"] == 7.77


def test_backfill_leaves_null_when_not_realized():
    """目标日后不足 horizon 个交易日 → 保持 NULL 等后续调度"""
    db = _mk_db()
    _add_kline(db, "000001", "2026-08-14", 10.0)
    _add_kline(db, "000001", "2026-08-17", 10.5)
    db.conn.execute(
        "INSERT INTO factor_daily (date, code, fwd_return_5d) "
        "VALUES ('2026-08-14', '000001', NULL)")
    db.conn.commit()
    ld = _loader(db)
    filled = ld.backfill_fwd_returns("2026-08-14", horizons=(5,))
    assert filled[5] == 0
    row = db.conn.execute(
        "SELECT fwd_return_5d FROM factor_daily "
        "WHERE date='2026-08-14' AND code='000001'").fetchone()
    assert row["fwd_return_5d"] is None

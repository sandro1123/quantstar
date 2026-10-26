"""DL-19: 复权元数据表 DDL + 幂等建表 — :memory: 库, 无生产写"""

import sqlite3

from data_layer.adjust_tables import ADJUST_TABLES, ensure_adjust_tables


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def test_ensure_creates_all_three_tables():
    db = _FakeDB()
    result = ensure_adjust_tables(db)
    assert result == {t: True for t in ADJUST_TABLES}


def test_ensure_idempotent():
    db = _FakeDB()
    ensure_adjust_tables(db)
    result = ensure_adjust_tables(db)  # 重跑不报错
    assert all(result.values())


def test_dividends_insert_and_pk():
    db = _FakeDB()
    ensure_adjust_tables(db)
    db.conn.execute(
        "INSERT INTO dividends (code, ex_date, cash_dividend) "
        "VALUES ('600519', '2026-06-30', 27.46)")
    db.conn.commit()
    row = db.conn.execute(
        "SELECT cash_dividend FROM dividends WHERE code='600519'").fetchone()
    assert row["cash_dividend"] == 27.46


def test_adjust_factors_insert_and_pk():
    db = _FakeDB()
    ensure_adjust_tables(db)
    db.conn.execute(
        "INSERT INTO adjust_factors (code, date, adjust_factor) "
        "VALUES ('600519', '2026-08-18', 1.35)")
    db.conn.commit()
    n = db.conn.execute("SELECT COUNT(*) FROM adjust_factors").fetchone()[0]
    assert n == 1


def test_splits_insert_and_pk():
    db = _FakeDB()
    ensure_adjust_tables(db)
    db.conn.execute(
        "INSERT INTO splits (code, ex_date, split_factor) "
        "VALUES ('300750', '2026-05-10', 2.0)")
    db.conn.commit()
    row = db.conn.execute(
        "SELECT split_factor FROM splits WHERE code='300750'").fetchone()
    assert row["split_factor"] == 2.0

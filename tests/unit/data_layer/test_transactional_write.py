"""DL-03 验收测试 — 事务性写入: 写入中途异常/校验不达标 → 表内无半批数据"""
import sqlite3

import pytest

from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.repos.kline import KlineRepo


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _make_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    return _FakeDB(conn)


def _records(n, date="2026-08-11"):
    return [{"code": f"{i:06d}", "date": date, "open": 1.0, "high": 1.1,
             "low": 0.9, "close": 1.0, "volume": 100.0, "amount": 100.0,
             "pre_close": 1.0, "change_pct": 0.0, "turnover_rate": 0.0}
            for i in range(n)]


def test_atomic_upsert_date_success(tmp_path):
    db = _make_db(tmp_path)
    repo = KlineRepo(db)
    n = repo.atomic_upsert_date("2026-08-11", _records(5), min_rows=3)
    assert n == 5
    cnt = db.get_connection().execute(
        "SELECT COUNT(*) FROM daily_klines WHERE date='2026-08-11'").fetchone()[0]
    assert cnt == 5


def test_atomic_upsert_date_min_rows_fail_rolls_back(tmp_path):
    """写入行数 < min_rows → 整体回滚, 表内不留半批"""
    db = _make_db(tmp_path)
    repo = KlineRepo(db)
    n = repo.atomic_upsert_date("2026-08-11", _records(2), min_rows=3)
    assert n == -1
    cnt = db.get_connection().execute(
        "SELECT COUNT(*) FROM daily_klines WHERE date='2026-08-11'").fetchone()[0]
    assert cnt == 0, "校验不达标应回滚, 表内不应有行"


def test_atomic_upsert_date_mid_write_exception_rolls_back(tmp_path):
    """executemany 中途异常 → 回滚, 表内无半批数据"""
    db = _make_db(tmp_path)
    repo = KlineRepo(db)
    records = _records(3)
    # 第 4 行 close 为不可绑定对象 → executemany 执行中抛错
    bad = dict(_records(1, date="2026-08-11")[0])
    bad["code"] = "999999"
    bad["close"] = object()
    records.append(bad)
    with pytest.raises(Exception):
        repo.atomic_upsert_date("2026-08-11", records, min_rows=0)
    cnt = db.get_connection().execute(
        "SELECT COUNT(*) FROM daily_klines WHERE date='2026-08-11'").fetchone()[0]
    assert cnt == 0, "写入中途异常应回滚, 表内不应有半批数据"


class _ExplodingPipeline(BasePipeline):
    """run() 内写入(未 commit)后抛异常"""
    name = "exploding"

    def __init__(self, db):
        self.db = db

    def run(self, input):
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_klines (
                code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
                volume REAL, amount REAL, pre_close REAL, change_pct REAL,
                turnover_rate REAL, PRIMARY KEY (code, date))
        """)
        conn.commit()
        conn.execute(
            "INSERT INTO daily_klines (code, date) VALUES ('000001', '2026-08-11')")
        # 未 commit 即抛异常
        raise RuntimeError("boom mid-write")


def test_base_execute_rolls_back_uncommitted_on_exception(tmp_path):
    """base.execute() 异常时回滚未提交写入 (D91)"""
    db = _make_db(tmp_path)
    pipe = _ExplodingPipeline(db)
    result = pipe.execute(PipelineInput(date="2026-08-11", db=db))
    assert result.status == "failed"
    assert "boom" in result.errors[0]
    cnt = db.get_connection().execute(
        "SELECT COUNT(*) FROM daily_klines WHERE date='2026-08-11'").fetchone()[0]
    assert cnt == 0, "execute() 异常应回滚未提交写入, 表内不应有半批数据"

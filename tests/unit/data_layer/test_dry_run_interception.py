"""DL-32: BasePipeline.execute() 基类统一拦截 dry_run。

契约: dry_run=True 时, 管线业务逻辑照跑, 但对目标表的写入一律被基类拦截丢弃,
目标表行数不变; 读取不受影响。拦截计数写入 result.metadata 以保证可观测 (铁律 #12)。

本文件分两部分:
- Part 1 基类机制: 用最小 fake 管线验证代理的写拦截/读透传/计数。
- Part 2 逐个管线: 17 个管线各一条断言 "dry_run=True 目标表行数不变"。
"""
import sqlite3

import pytest

from common.database import Database
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult


@pytest.fixture
def db(test_db_path):
    """独立临时库 (conftest 已隔离生产库); 每测重建 tgt 表保证行数断言干净。"""
    d = Database(test_db_path)
    conn = d.get_connection()
    conn.execute("DROP TABLE IF EXISTS tgt")
    conn.execute(
        "CREATE TABLE tgt (code TEXT, date TEXT, v REAL, PRIMARY KEY(code, date))"
    )
    conn.commit()
    return d


def _count(db, table="tgt"):
    return db.get_connection().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class _WriterPipeline(BasePipeline):
    """最小写入管线: 读一行再写一行, 用于验证代理。"""

    name = "writer"

    def __init__(self, db=None):
        self.db = db

    def run(self, input):
        db = input.db or self.db
        conn = db.get_connection()
        # 读 (应透传)
        existing = conn.execute("SELECT COUNT(*) FROM tgt").fetchone()[0]
        # 写 (dry_run 时应被拦截)
        conn.execute("INSERT OR REPLACE INTO tgt (code, date, v) VALUES ('A', ?, 1.0)", (input.date,))
        conn.commit()
        return PipelineResult(name=self.name, date=input.date, status="success",
                              records_written=1, metadata={"existing": existing})


class TestBaseDryRunInterception:
    def test_dry_run_false_writes(self, db):
        pipe = _WriterPipeline()
        res = pipe.execute(PipelineInput(date="2026-08-22", db=db, dry_run=False))
        assert res.status == "success"
        assert _count(db) == 1

    def test_dry_run_true_blocks_write_row_count_unchanged(self, db):
        pipe = _WriterPipeline()
        res = pipe.execute(PipelineInput(date="2026-08-22", db=db, dry_run=True))
        assert res.status == "success"
        assert _count(db) == 0, "dry_run=True 时目标表行数不得变化"

    def test_dry_run_true_reports_blocked_writes(self, db):
        pipe = _WriterPipeline()
        res = pipe.execute(PipelineInput(date="2026-08-22", db=db, dry_run=True))
        assert res.metadata.get("dry_run") is True
        assert res.metadata.get("dry_run_writes_blocked", 0) >= 1

    def test_dry_run_reads_still_work(self, db):
        # 预置一行, dry_run 时读取应透传看到它
        db.get_connection().execute("INSERT INTO tgt (code, date, v) VALUES ('Z', '2026-01-01', 9)")
        db.get_connection().commit()
        pipe = _WriterPipeline()
        res = pipe.execute(PipelineInput(date="2026-08-22", db=db, dry_run=True))
        assert res.metadata["existing"] == 1
        assert _count(db) == 1  # 只有预置行, 写入被拦

    def test_dry_run_covers_self_db_path(self, db):
        """news.py 用 self.db 而非 input.db; 基类必须同时接管 self.db。"""
        pipe = _WriterPipeline(db=db)

        class SelfDbWriter(_WriterPipeline):
            def run(self, input):
                conn = self.db.get_connection()  # 只走 self.db
                conn.execute("INSERT OR REPLACE INTO tgt (code, date, v) VALUES ('B', ?, 2.0)", (input.date,))
                conn.commit()
                return PipelineResult(name=self.name, date=input.date, status="success", records_written=1)

        p = SelfDbWriter(db=db)
        res = p.execute(PipelineInput(date="2026-08-22", dry_run=True))  # 不传 input.db
        assert res.status == "success"
        assert _count(db) == 0, "self.db 路径的写入也必须被拦截"

    def test_dry_run_executemany_blocked(self, db):
        class BulkWriter(BasePipeline):
            name = "bulk"
            def run(self, input):
                conn = (input.db or self.db).get_connection()
                conn.executemany("INSERT OR REPLACE INTO tgt (code, date, v) VALUES (?, ?, 1)",
                                 [(f"C{i}", input.date) for i in range(5)])
                conn.commit()
                return PipelineResult(name=self.name, date=input.date, status="success", records_written=5)

        res = BulkWriter().execute(PipelineInput(date="2026-08-22", db=db, dry_run=True))
        assert _count(db) == 0
        assert res.metadata.get("dry_run_writes_blocked", 0) >= 1

"""P-DATA-A1 测试 — KlinePipeline change_pct 重算(复权口径一致性)"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from common.database import Database
from data_layer._models import Kline
from data_layer.pipelines.kline import KlinePipeline


class TestRecomputeChangePct:
    def _make_repo(self, seed_rows=None):
        tmp = tempfile.TemporaryDirectory()
        db = Database(str(Path(tmp.name) / "test.db"))
        conn = db.get_connection()
        conn.execute("""
            CREATE TABLE daily_klines (
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
        for r in seed_rows or []:
            conn.execute("""
                INSERT INTO daily_klines (code,date,close) VALUES (?,?,?)
            """, (r[0], r[1], r[2]))
        conn.commit()
        return db

    def test_recompute_uses_db_prev_close(self):
        db = self._make_repo([("600000", "2026-07-01", 10.0)])
        pipe = KlinePipeline()
        klines = [
            Kline(code="600000", date="2026-07-02", open=10.5, high=10.6, low=10.4,
                  close=11.0, volume=100, amount=1000, pre_close=0.0, change_pct=-25.0),
        ]
        out = pipe._recompute_change_pct(db, "600000", klines)
        # 11.0 vs 前日 10.0 -> +10%
        assert out[0].change_pct == pytest.approx(10.0)
        assert out[0].pre_close == pytest.approx(10.0)

    def test_recompute_sequential_within_batch(self):
        db = self._make_repo()
        pipe = KlinePipeline()
        klines = [
            Kline(code="600000", date="2026-07-01", open=10, high=10, low=10,
                  close=10.0, volume=0, amount=0),
            Kline(code="600000", date="2026-07-02", open=11, high=11, low=11,
                  close=11.0, volume=0, amount=0),
        ]
        out = pipe._recompute_change_pct(db, "600000", klines)
        assert out[0].change_pct == 0.0  # 首日无前值
        assert out[1].change_pct == pytest.approx(10.0)  # 11/10-1

    def test_first_day_no_prev_close(self):
        db = self._make_repo()
        pipe = KlinePipeline()
        klines = [
            Kline(code="600000", date="2010-01-04", open=5, high=5, low=5,
                  close=5.0, volume=0, amount=0, pre_close=4.8, change_pct=99.0),
        ]
        out = pipe._recompute_change_pct(db, "600000", klines)
        assert out[0].change_pct == 0.0
        assert out[0].pre_close == pytest.approx(4.8)  # 保留源 pre_close 作参考

    def test_sorted_by_date(self):
        db = self._make_repo([("600000", "2026-07-01", 10.0)])
        pipe = KlinePipeline()
        klines = [
            Kline(code="600000", date="2026-07-03", open=12, high=12, low=12,
                  close=12.0, volume=0, amount=0),
            Kline(code="600000", date="2026-07-02", open=11, high=11, low=11,
                  close=11.0, volume=0, amount=0),
        ]
        out = pipe._recompute_change_pct(db, "600000", klines)
        assert [k.date for k in out] == ["2026-07-02", "2026-07-03"]
        assert out[0].change_pct == pytest.approx(10.0)   # 11 vs 10
        assert out[1].change_pct == pytest.approx(9.09, abs=0.01)  # 12 vs 11

    def test_recompute_with_repo_container(self):
        """P-DATA-A2 回归: 真实生产形态传入 RepoContainer, 必须走 repo.kline.db 取连接"""
        from data_layer.repos import get_repo
        db = self._make_repo([("600000", "2026-07-01", 10.0)])
        repo = get_repo(db)
        pipe = KlinePipeline()
        klines = [
            Kline(code="600000", date="2026-07-02", open=10.5, high=10.6, low=10.4,
                  close=11.0, volume=100, amount=1000, pre_close=0.0, change_pct=-25.0),
        ]
        out = pipe._recompute_change_pct(repo, "600000", klines)
        assert out[0].change_pct == pytest.approx(10.0)
        assert out[0].pre_close == pytest.approx(10.0)

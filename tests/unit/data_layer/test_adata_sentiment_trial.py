# -*- coding: utf-8 -*-
"""卡C AdataSentiment 通电试 TDD（用户批「通电」sm-c2e803933cfa42b9）

管道已在（09-12，DDL 自建 + 诚实降级，b 案单腿），本卡做**通电**：
ops.pipelines 新段 + 试跑 3 交易日观察。判据四问：
  ① 正常产出：有源 → 人气榜/概念行落表，degraded=False
  ② 无源诚实降级：源不可得 → 零行 + degraded=True（不造假）
  ③ 幂等：同日重跑 INSERT OR REPLACE，行数不翻倍
  ④ 种子不干扰：09-12 种子行不被误删/污染（新日期写入正交）
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))


class FakeSource:
    """假源：可控返回，模拟有数据/空数据两态"""

    def __init__(self, pop=None, hot=None, name="adata"):
        self._pop, self._hot, self.name = pop, hot, name

    def get_data(self, kind, params=None):
        if kind == "popularity":
            return self._pop or []
        if kind == "concept_hot":
            return self._hot or []
        return []


class FakeRegistry:
    def __init__(self, source):
        self._s = source

    def get_best_source(self, kind):
        return self._s


class FakeDB:
    def __init__(self, conn):
        self._c = conn

    def get_connection(self):
        return self._c


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _pipe(conn, source):
    from data_layer.pipelines.adata_sentiment_pipeline import AdataSentimentPipeline
    return AdataSentimentPipeline(
        db=FakeDB(conn),
        source_registry=FakeRegistry(source) if source else None)


class TestNormalProduce:
    """① 正常产出：有源 → 落表 + degraded=False"""

    def test_popularity_and_hot_rows_written(self, conn):
        src = FakeSource(
            pop=[{"code": "600000", "rank": 1, "his_rank": 2, "source": "adata"},
                 {"code": "000001", "rank": 2, "his_rank": 1, "source": "adata"}],
            hot=[{"rank": 1, "concept_code": "BK001", "concept_name": "AI",
                  "change_pct": 3.2, "hot_value": 99.0, "hot_tag": "热"}])
        r = _pipe(conn, src).run({"date": "2026-09-25"})
        assert r["degraded"] is False
        assert r["popularity_rows"] == 2 and r["concept_hot_rows"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM popularity_daily WHERE date=?",
            ("2026-09-25",)).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM hot_concept_daily WHERE date=?",
            ("2026-09-25",)).fetchone()[0] == 1

    def test_fields_mapped_correctly(self, conn):
        src = FakeSource(pop=[{"code": "600519", "rank": 7, "his_rank": 3}], hot=[])
        _pipe(conn, src).run({"date": "2026-09-25"})
        row = conn.execute(
            "SELECT * FROM popularity_daily WHERE date=?",
            ("2026-09-25",)).fetchone()
        assert row["code"] == "600519" and row["rank"] == 7 and row["his_rank"] == 3


class TestHonestDegrade:
    """② 无源诚实降级：零行 + degraded=True，不造假"""

    def test_no_source_degrades_with_zero_rows(self, conn):
        r = _pipe(conn, None).run({"date": "2026-09-25"})
        assert r["degraded"] is True
        assert r["popularity_rows"] == 0 and r["concept_hot_rows"] == 0
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM popularity_daily WHERE date=?",
                ("2026-09-25",)).fetchone()[0]
            assert n == 0
        except sqlite3.OperationalError:
            pass   # 表未建也合规（降级早退）

    def test_empty_payload_not_fabricated(self, conn):
        """源在但返空 → 零行，degraded 仍 False（源活着只是没数据，两态可区分）"""
        r = _pipe(conn, FakeSource(pop=[], hot=[])).run({"date": "2026-09-25"})
        assert r["popularity_rows"] == 0
        assert r["degraded"] is False


class TestIdempotency:
    """③ 幂等：同日重跑行数不翻倍"""

    def test_same_day_rerun_no_duplication(self, conn):
        src = FakeSource(
            pop=[{"code": "600000", "rank": 1, "his_rank": 1}],
            hot=[{"rank": 1, "concept_code": "BK001", "concept_name": "AI"}])
        p = _pipe(conn, src)
        p.run({"date": "2026-09-25"})
        p.run({"date": "2026-09-25"})
        assert conn.execute(
            "SELECT COUNT(*) FROM popularity_daily WHERE date=?",
            ("2026-09-25",)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM hot_concept_daily WHERE date=?",
            ("2026-09-25",)).fetchone()[0] == 1

    def test_rerun_updates_values(self, conn):
        """INSERT OR REPLACE 语义：rank 变化应被更新而非留旧值"""
        _pipe(conn, FakeSource(pop=[{"code": "600000", "rank": 5, "his_rank": 5}])
              ).run({"date": "2026-09-25"})
        _pipe(conn, FakeSource(pop=[{"code": "600000", "rank": 1, "his_rank": 1}])
              ).run({"date": "2026-09-25"})
        row = conn.execute(
            "SELECT rank FROM popularity_daily WHERE date=? AND code=?",
            ("2026-09-25", "600000")).fetchone()
        assert row["rank"] == 1


class TestSeedNonInterference:
    """④ 09-12 种子不被干扰（新日期写入正交）"""

    def test_seed_rows_untouched(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS popularity_daily ("
            "date TEXT NOT NULL, code TEXT NOT NULL, rank INTEGER, "
            "his_rank REAL, source TEXT DEFAULT 'adata', "
            "created_at TEXT, PRIMARY KEY (date, code))")
        conn.execute(
            "INSERT INTO popularity_daily (date, code, rank, his_rank) "
            "VALUES ('2026-09-12', '600000', 99, 99)")
        conn.commit()
        src = FakeSource(pop=[{"code": "000001", "rank": 1, "his_rank": 1}])
        _pipe(conn, src).run({"date": "2026-09-25"})
        seed = conn.execute(
            "SELECT rank FROM popularity_daily WHERE date=?",
            ("2026-09-12",)).fetchone()
        assert seed["rank"] == 99
        assert conn.execute(
            "SELECT COUNT(*) FROM popularity_daily").fetchone()[0] == 2


class TestPipelineRegistration:
    """调度注册（提案面）：yaml 段存在且参数合规"""

    def test_yaml_section_exists(self):
        import yaml
        cfg = yaml.safe_load(open("config/defaults/ops.yaml", encoding="utf-8"))
        pipes = cfg.get("ops", {}).get("pipelines", {})
        assert "adata_sentiment" in pipes, "须注册 ops.pipelines.adata_sentiment"

    def test_schedule_avoids_lock_congestion_band(self):
        """避开 16:00-17:35 堵塞带（DDP 持锁 2760-5220s 定谳）"""
        import yaml
        cfg = yaml.safe_load(open("config/defaults/ops.yaml", encoding="utf-8"))
        p = cfg["ops"]["pipelines"]["adata_sentiment"]
        h, m = (int(x) for x in p["schedule"].split(":"))
        minutes = h * 60 + m
        assert not (16 * 60 <= minutes <= 17 * 60 + 35), \
            f"调度 {p['schedule']} 落在 16:00-17:35 堵塞带内"
        assert 18 * 60 + 30 <= minutes <= 19 * 60 + 30, \
            f"调度 {p['schedule']} 应在夜间窗 18:30-19:30"

    def test_has_wait_timeout_and_flock_semantics(self):
        import yaml
        cfg = yaml.safe_load(open("config/defaults/ops.yaml", encoding="utf-8"))
        p = cfg["ops"]["pipelines"]["adata_sentiment"]
        assert int(p.get("wait_timeout_s", 0)) > 0, "须显式 wait_timeout_s"
        assert int(p.get("timeout_s", 0)) > 0

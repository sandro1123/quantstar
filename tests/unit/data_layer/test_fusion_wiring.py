"""DL-01 验收测试 — FusionStrategy 接线 + code 键对齐 + 管线告警传递"""
import sqlite3

import pytest

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.lhb import LHBPipeline
from data_layer.pipelines.limit_up_down import LimitUpDownPipeline
from data_layer.sources.fusion import FusionStrategy


class _FakeSource:
    """假数据源: 按 data_type 返回预设数据"""

    def __init__(self, name: str, data: dict, available: bool = True):
        self.name = name
        self._data = data
        self._available = available

    @property
    def capabilities(self):
        return set(self._data.keys())

    def is_available(self) -> bool:
        return self._available

    def get_data(self, data_type: str, params: dict):
        return self._data.get(data_type)


class _FakeRegistry:
    def __init__(self, sources):
        self._sources = sources

    def get_sources_for(self, data_type: str):
        return [s for s in self._sources
                if data_type in s.capabilities and s.is_available()]


class _FakeDB:
    """包装裸 sqlite3 连接, 提供管线所需的 get_connection()"""

    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _make_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.execute("""
        CREATE TABLE stock_limit_up_down (
            code TEXT, date TEXT, name TEXT, limit_type TEXT,
            price REAL, change_pct REAL, count INTEGER,
            PRIMARY KEY (code, date, limit_type)
        )
    """)
    conn.execute("""
        CREATE TABLE lhb_daily (
            code TEXT, date TEXT, net_buy_amount REAL DEFAULT 0,
            total_buy REAL DEFAULT 0, total_sell REAL DEFAULT 0,
            PRIMARY KEY (code, date)
        )
    """)
    conn.commit()
    return _FakeDB(conn)


def test_fuse_two_sources_numeric_diff_produces_warnings():
    """验收: 两个数值差异超阈值的假源 → ValidationReport.warnings 非空"""
    s1 = _FakeSource("src_a", {"limit_up": [
        {"code": "600519", "price": 100.0, "change_pct": 10.0},
    ]})
    s2 = _FakeSource("src_b", {"limit_up": [
        {"code": "600519", "price": 120.0, "change_pct": 10.0},
    ]})
    fusion = FusionStrategy(_FakeRegistry([s1, s2]))
    data, report = fusion.fuse("limit_up", {"date": "2026-08-18"}, key_field="code")
    assert data is not None
    assert report is not None
    assert report.has_warnings
    assert any("price" in w for w in report.warnings)


def test_fuse_code_alignment_no_false_positive_on_reorder():
    """code 键对齐: 两源排序不同但数值一致 → 不应产生差异告警"""
    s1 = _FakeSource("src_a", {"limit_up": [
        {"code": "000001", "price": 10.0},
        {"code": "600519", "price": 100.0},
    ]})
    s2 = _FakeSource("src_b", {"limit_up": [
        {"code": "600519", "price": 100.0},
        {"code": "000001", "price": 10.0},
    ]})
    fusion = FusionStrategy(_FakeRegistry([s1, s2]))
    data, report = fusion.fuse("limit_up", {"date": "2026-08-18"}, key_field="code")
    assert data is not None
    assert report is not None
    assert not report.has_warnings, f"排序不同不应误报: {report.warnings}"


def test_fuse_index_alignment_still_works_without_key_field():
    """不指定 key_field 时保持 index 对齐 (兼容旧行为)"""
    s1 = _FakeSource("src_a", {"limit_up": [{"code": "000001", "price": 10.0}]})
    s2 = _FakeSource("src_b", {"limit_up": [{"code": "000001", "price": 15.0}]})
    fusion = FusionStrategy(_FakeRegistry([s1, s2]))
    data, report = fusion.fuse("limit_up", {"date": "2026-08-18"})
    assert report is not None and report.has_warnings


def test_fuse_best_source_failure_falls_back():
    """最佳源查询失败时回退到第一个成功的源, 不丢数据"""
    class _FailSource(_FakeSource):
        def get_data(self, data_type, params):
            raise RuntimeError("boom")

    s1 = _FailSource("src_a", {"limit_up": []})
    s2 = _FakeSource("src_b", {"limit_up": [{"code": "000001", "price": 10.0}]})
    fusion = FusionStrategy(_FakeRegistry([s1, s2]))
    data, report = fusion.fuse("limit_up", {"date": "2026-08-18"}, key_field="code")
    assert data == [{"code": "000001", "price": 10.0}]


def test_limit_up_down_pipeline_warnings_propagate(tmp_path):
    """管线走 fusion 时, 多源差异告警进入 PipelineResult.warnings (不阻塞, D198)"""
    db = _make_db(tmp_path)
    s1 = _FakeSource("src_a", {
        "limit_up": [{"code": "600519", "limit_type": "up", "price": 100.0, "change_pct": 10.0, "count": 1}],
        "limit_down": [],
    })
    s2 = _FakeSource("src_b", {
        "limit_up": [{"code": "600519", "limit_type": "up", "price": 130.0, "change_pct": 10.0, "count": 1}],
        "limit_down": [],
    })
    registry = _FakeRegistry([s1, s2])
    fusion = FusionStrategy(registry)
    pipe = LimitUpDownPipeline(db=None, source_registry=registry, fusion=fusion)
    result = pipe.run(PipelineInput(date="2026-08-18", db=db))
    assert result.status == "success"
    assert result.records_written >= 1
    assert result.warnings, "多源差异告警应写入 PipelineResult.warnings"
    db.get_connection().close()


def test_lhb_pipeline_warnings_propagate(tmp_path):
    """lhb 管线走 fusion 时告警进入 PipelineResult.warnings"""
    db = _make_db(tmp_path)
    s1 = _FakeSource("src_a", {"lhb": [{"code": "600519", "buy_in": 100.0, "turnover": 200.0, "capitalization": 50.0}]})
    s2 = _FakeSource("src_b", {"lhb": [{"code": "600519", "buy_in": 300.0, "turnover": 200.0, "capitalization": 50.0}]})
    registry = _FakeRegistry([s1, s2])
    fusion = FusionStrategy(registry)
    pipe = LHBPipeline(db=None, source_registry=registry, fusion=fusion)
    result = pipe.run(PipelineInput(date="2026-08-18", db=db))
    assert result.status == "success"
    assert result.records_written == 1
    assert result.warnings, "多源差异告警应写入 PipelineResult.warnings"
    db.get_connection().close()


def test_limit_up_down_pipeline_no_fusion_fallback(tmp_path):
    """无 fusion 时保持原逐源 fallback 行为 (回归保护)"""
    db = _make_db(tmp_path)
    s1 = _FakeSource("src_a", {
        "limit_up": [{"code": "000001", "limit_type": "up", "price": 10.0, "change_pct": 5.0, "count": 1}],
        "limit_down": [],
    })
    pipe = LimitUpDownPipeline(db=None, source_registry=_FakeRegistry([s1]), fusion=None)
    result = pipe.run(PipelineInput(date="2026-08-18", db=db))
    assert result.status == "success"
    assert result.warnings == []
    db.get_connection().close()

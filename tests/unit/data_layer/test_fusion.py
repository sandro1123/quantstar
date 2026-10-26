"""测试多源融合策略"""

import pytest
from data_layer.sources.fusion import FusionStrategy, ValidationReport
from data_layer.sources.registry import SourceRegistry


class MockSource:
    def __init__(self, name: str, caps: set[str], data: dict = None):
        self._name = name
        self._caps = caps
        self._data = data or {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> set[str]:
        return self._caps

    def is_available(self) -> bool:
        return True

    def get_data(self, data_type: str, params: dict):
        return self._data


def test_fusion_single_source():
    registry = SourceRegistry()
    registry.register(MockSource("source1", {"kline"}, {"close": 100}))

    fusion = FusionStrategy(registry)
    data, report = fusion.fuse("kline", {})

    assert data == {"close": 100}
    assert report is None


def test_fusion_multiple_sources_match():
    registry = SourceRegistry()
    registry.register(MockSource("source1", {"kline"}, {"close": 100}))
    registry.register(MockSource("source2", {"kline"}, {"close": 100}))

    fusion = FusionStrategy(registry)
    data, report = fusion.fuse("kline", {})

    assert data == {"close": 100}
    assert report is not None
    assert not report.has_warnings


def test_fusion_multiple_sources_differ():
    registry = SourceRegistry()
    registry.register(MockSource("source1", {"kline"}, {"close": 100}))
    registry.register(MockSource("source2", {"kline"}, {"close": 110}))

    fusion = FusionStrategy(registry)
    data, report = fusion.fuse("kline", {}, threshold=0.05)

    assert data == {"close": 100}
    assert report.has_warnings


def test_validation_report():
    report = ValidationReport(data_type="kline")
    report.add_difference("close", "source1", 100, "source2", 110, threshold=0.05)

    assert report.has_warnings
    assert len(report.differences) == 1


# ---------------------------------------------------------------------------
# 卡① 融合空容器回退 (2026-09-15 lhb 断采定谳 A)
# ---------------------------------------------------------------------------
# 病理: fuse() 的 best_data 回退只判 `is None`, 主源返回**空列表**时
# 空容器「胜出」, 备源的真数据被丢弃。
# 09-14 实证: zzshare 腿实有 72 行, 因 fuyao 腿返 [] 而被弃, 当日报
# "所有源均无数据"; 09-15 16:02 同族 (首源 adata 返非空错源数据胜出)。
# 修: 空容器与 None 同等对待, 顺延下一源。


class _SeqSource:
    """按 data_type 返回预设数据的源 (可返空列表, 与 MockSource 的 dict 不同)。"""

    def __init__(self, name: str, payload, caps: set[str] = None):
        self._name = name
        self._payload = payload
        self._caps = caps or {"lhb"}

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> set[str]:
        return self._caps

    def is_available(self) -> bool:
        return True

    def get_data(self, data_type: str, params: dict):
        return self._payload


def test_fuse_empty_list_primary_falls_back_to_backup():
    """主源返空列表 [] → 必须顺延到备源, 不得让空容器胜出 (09-14 zzshare 72 行被弃案)。"""
    registry = SourceRegistry()
    registry.register(_SeqSource("primary", []))
    registry.register(_SeqSource("backup", [{"code": "600519", "buy_in": 100.0}]))

    fusion = FusionStrategy(registry)
    data, report = fusion.fuse("lhb", {}, key_field="code")

    assert data == [{"code": "600519", "buy_in": 100.0}], (
        f"主源空列表时备源真数据必须胜出, 实际 data={data!r}"
    )


def test_fuse_empty_dict_primary_falls_back_to_backup():
    """空容器不止 list — 空 dict/tuple 同样不得胜出。"""
    registry = SourceRegistry()
    registry.register(_SeqSource("primary", {}, caps={"kline"}))
    registry.register(_SeqSource("backup", {"code": "600519", "buy_in": 100.0}, caps={"kline"}))

    fusion = FusionStrategy(registry)
    data, report = fusion.fuse("kline", {})

    assert data == {"code": "600519", "buy_in": 100.0}


def test_fuse_all_sources_empty_returns_empty_not_none():
    """全源皆空 → 返回空容器 (调用方按 falsy 判"无数据"), 不得抛异常。"""
    registry = SourceRegistry()
    registry.register(_SeqSource("primary", []))
    registry.register(_SeqSource("backup", []))

    fusion = FusionStrategy(registry)
    data, report = fusion.fuse("lhb", {}, key_field="code")

    assert not data, f"全源空时应返回空容器, 实际 {data!r}"


def test_fuse_nonempty_primary_unchanged():
    """不变量锚: 主源非空时逐字等价 (防卡①改动把正常路径改坏)。"""
    payload = [{"code": "600519", "buy_in": 100.0}]
    registry = SourceRegistry()
    registry.register(_SeqSource("primary", payload))
    registry.register(_SeqSource("backup", [{"code": "000001", "buy_in": 1.0}]))

    fusion = FusionStrategy(registry)
    data, report = fusion.fuse("lhb", {}, key_field="code")

    assert data == payload, "主源非空时必须逐字返回主源数据 (不变量锚)"


def test_fuse_single_source_error_reports_source_error():
    """单源路径源抛错时, 必须留痕 source_errors (旧实现只 log 后返回 (None, None),
    上游无法区分"源报错"与"源干净返空" → failed 退化为 partial)。"""
    class _Boom:
        name = "only"
        capabilities = {"lhb"}
        def is_available(self): return True
        def get_data(self, data_type, params):
            raise RuntimeError("boom-429")

    registry = SourceRegistry()
    registry.register(_Boom())
    fusion = FusionStrategy(registry)
    data, report = fusion.fuse("lhb", {})

    assert data is None
    assert report is not None, "源抛错必须返回带病因的报告 (不得 (None, None))"
    assert report.source_errors.get("only"), f"须留痕源名与错因, 实际 {report.source_errors!r}"

"""源能力矩阵 (coverage) 判据钉 — 2026-11 对标 DSA「市场支持边界」的代码化版。

四条状态各钉一条 + 探测异常不中断 + 汇总守恒 + 缺口过滤。
"""
from data_layer.sources.coverage import (
    STATUS_DEGRADED,
    STATUS_GAP,
    STATUS_OK,
    STATUS_SINGLE,
    assert_coverage,
    coverage_report,
    gaps,
)
from data_layer.sources.registry import SourceRegistry


class _Src:
    """最小 DataSource 替身: name / capabilities / is_available / get_data。"""

    def __init__(self, name, caps, available=True, boom=False):
        self.name = name
        self.capabilities = set(caps)
        self._available = available
        self._boom = boom

    def is_available(self):
        if self._boom:
            raise RuntimeError("probe failed")
        return self._available

    def get_data(self, data_type, params):
        return []


def _reg(*sources):
    reg = SourceRegistry()
    for s in sources:
        reg.register(s)
    return reg


def _entry(report, data_type):
    for e in report["types"]:
        if e["data_type"] == data_type:
            return e
    raise AssertionError(f"矩阵缺 {data_type} — DATA_TYPES 与矩阵不同步")


def test_two_available_sources_is_ok():
    """>= 2 条腿 = ok (宪法#5 单点必有对冲)。"""
    reg = _reg(_Src("a", ["kline"]), _Src("b", ["kline"]))
    e = _entry(coverage_report(reg), "kline")
    assert e["status"] == STATUS_OK
    assert sorted(e["available"]) == ["a", "b"]
    assert e["unavailable"] == []


def test_single_source_is_flagged_not_ok():
    """恰 1 条腿必须显式标 single —— 能跑但宪法#5 未满足, 不得混进 ok。"""
    reg = _reg(_Src("a", ["kline"]))
    e = _entry(coverage_report(reg), "kline")
    assert e["status"] == STATUS_SINGLE
    assert e["available"] == ["a"]


def test_registered_but_unavailable_is_degraded():
    """有源声明能力但当前不可用 (凭证失效/熔断) → degraded, 与 gap 区分。"""
    reg = _reg(_Src("a", ["financial"], available=False))
    e = _entry(coverage_report(reg), "financial")
    assert e["status"] == STATUS_DEGRADED
    assert e["available"] == []
    assert e["unavailable"] == ["a"]


def test_no_source_is_gap():
    """没有任何源声明该能力 → gap (配了也用不了)。"""
    reg = _reg(_Src("a", ["kline"]))
    e = _entry(coverage_report(reg), "tick_data")
    assert e["status"] == STATUS_GAP
    assert e["registered"] == 0


def test_probe_exception_marks_unavailable_not_crash():
    """单源 is_available() 抛异常不得中断整张矩阵, 该源按不可用计 (fail-visible)。"""
    reg = _reg(_Src("a", ["kline"], boom=True), _Src("b", ["kline"]))
    report = coverage_report(reg)
    e = _entry(report, "kline")
    assert e["available"] == ["b"]
    assert e["unavailable"] == ["a"]
    assert "a" in report["unhealthy_sources"]


def test_summary_conserves_total():
    """四态计数之和必须等于类型总数 (不漏不重)。"""
    reg = _reg(_Src("a", ["kline"]), _Src("b", ["valuation"], available=False))
    report = coverage_report(reg)
    assert sum(report["summary"].values()) == report["total_types"]
    assert report["total_types"] == len(report["types"])


def test_gaps_filters_to_required():
    """required 只关心实际要用的类型, 不把全表缺口都算进来。"""
    reg = _reg(_Src("a", ["kline"]), _Src("b", ["kline"]))
    assert gaps(reg, ["kline"]) == []
    assert gaps(reg, ["kline", "valuation"]) == ["valuation"]


def test_gaps_flags_unknown_type_name():
    """拼错/未登记的类型名算缺口 —— 不静默通过 (宁红不默)。"""
    reg = _reg(_Src("a", ["kline"]), _Src("b", ["kline"]))
    assert gaps(reg, ["kline", "kline_typo"]) == ["kline_typo"]
    assert assert_coverage(reg, ["nope"]) == ["nope"]


def test_gap_sorts_before_ok():
    """报告按严重度排序: 缺口在最前, ok 在最后 (先看坏消息)。"""
    reg = _reg(_Src("a", ["kline"]), _Src("b", ["kline"]))
    report = coverage_report(reg)
    statuses = [e["status"] for e in report["types"]]
    assert statuses[0] == STATUS_GAP, f"首条应为缺口: {statuses[:3]}"
    assert statuses[-1] == STATUS_OK, f"末条应为 ok: {statuses[-3:]}"

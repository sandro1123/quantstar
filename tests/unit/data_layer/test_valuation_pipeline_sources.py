"""ValuationPipeline 多源批量合并、有效覆盖门禁与 None 保持测试。"""

import math

import pytest

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.valuation import ValuationPipeline
from data_layer.sources.registry import SourceRegistry


class CaptureValuationRepo:
    def __init__(self):
        self.writes = []

    def batch_upsert(self, records):
        self.writes.append(list(records))


class FakeRepo:
    def __init__(self):
        self.valuation = CaptureValuationRepo()


class BatchSource:
    def __init__(self, name, records):
        self.name = name
        self.capabilities = {"historical_valuation"}
        self.records = records
        self.batch_calls = []

    def is_available(self):
        return True

    def get_batch(self, data_type, params):
        self.batch_calls.append((data_type, list(params["symbols"])))
        requested = set(params["symbols"])
        return [record for record in self.records if record["code"] in requested]

    def get_data(self, data_type, params):
        return None


class ExplodingSource:
    def __init__(self, name):
        self.name = name
        self.capabilities = {"historical_valuation"}

    def is_available(self):
        return True

    def get_batch(self, data_type, params):
        raise TimeoutError("probe timeout")

    def get_data(self, data_type, params):
        raise TimeoutError("probe timeout")


def make_registry(*sources):
    registry = SourceRegistry()
    for index, source in enumerate(sources, start=1):
        registry.register(source, priority=index)
    return registry


def run_pipeline(monkeypatch, sources, symbols, **kwargs):
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    pipeline = ValuationPipeline(make_registry(*sources))
    result = pipeline.run(
        PipelineInput(
            date="2026-07-28",
            symbols=symbols,
            **kwargs,
        )
    )
    return repo, result


def test_partial_bulk_result_is_merged_with_next_source(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    primary = BatchSource(
        "primary",
        [
            {"code": "600000", "date": "20260728", "pe_ttm": 6.0},
            {"code": "000001", "date": "2026-07-28", "pe_ttm": 7.0},
        ],
    )
    fallback = BatchSource(
        "fallback",
        [{"code": "920002", "date": "2026-07-28", "pe_ttm": 8.0}],
    )
    registry = make_registry(primary, fallback)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=["600000", "000001", "920002"],
            config={"min_coverage_ratio": 1.0},
        )
    )

    assert result.status == "success"
    assert result.records_written == 3
    assert result.metadata["coverage_ratio"] == 1.0
    assert result.metadata["source_counts"] == {"primary": 2, "fallback": 1}
    assert fallback.batch_calls == [("historical_valuation", ["920002"])]
    assert len(repo.valuation.writes) == 1
    assert {record["date"] for record in repo.valuation.writes[0]} == {"2026-07-28"}


def test_low_coverage_fails_without_writing(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    partial = BatchSource(
        "partial",
        [{"code": "600000", "date": "2026-07-28", "pe_ttm": 6.0}],
    )
    registry = make_registry(partial)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=["600000", "000001", "920002"],
            config={"min_coverage_ratio": 0.95, "max_individual_fallback": 0},
        )
    )

    assert result.status == "failed"
    assert result.records_read == 1
    assert result.records_written == 0
    assert result.metadata["quality_status"] == "failed"
    assert result.metadata["missing_count"] == 2
    assert "below gate" in result.errors[0]
    assert repo.valuation.writes == []


def test_dry_run_validates_but_does_not_write(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    source = BatchSource(
        "source",
        [{"code": "600000", "date": "2026-07-28", "pe_ttm": 6.0}],
    )
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=["600000"],
            dry_run=True,
            config={"min_coverage_ratio": 1.0},
        )
    )

    assert result.status == "success"
    assert result.records_read == 1
    assert result.records_written == 0
    assert repo.valuation.writes == []


def test_segment_classification_68x_sh_92x_bj(monkeypatch):
    """Verify 688xxx → SH, 920xxx → BJ, 600xxx → SH, 000xxx → SZ."""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    source = BatchSource(
        "source",
        [
            {"code": "688001", "date": "20260728", "pe_ttm": 6.0},
            {"code": "920002", "date": "20260728", "pe_ttm": 8.0},
            {"code": "600000", "date": "20260728", "pe_ttm": 7.0},
            {"code": "000001", "date": "20260728", "pe_ttm": 5.0},
        ],
    )
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=["688001", "920002", "600000", "000001"],
            config={"min_coverage_ratio": 1.0},
        )
    )

    assert result.status == "success"
    sc = result.metadata["segment_coverage"]
    assert sc["SH"]["expected"] == 2  # 688001 + 600000
    assert sc["SH"]["covered"] == 2
    assert sc["SH"]["coverage_ratio"] == 1.0
    assert sc["BJ"]["expected"] == 1  # 920002
    assert sc["BJ"]["covered"] == 1
    assert sc["SZ"]["expected"] == 1  # 000001
    assert sc["SZ"]["covered"] == 1
    assert result.metadata["failed_segments"] == []


def test_whole_segment_missing_fails_zero_write(monkeypatch):
    """One segment fully missing: overall coverage passes but segment gate fails — zero-write."""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    sh_symbols = [f"600{i:03d}" for i in range(10)]
    sz_symbols = ["000001"]
    bj_symbols = ["920002"]
    all_symbols = sh_symbols + sz_symbols + bj_symbols

    sh_records = [{"code": c, "date": "20260728", "pe_ttm": 6.0} for c in sh_symbols]
    sz_records = [{"code": c, "date": "20260728", "pe_ttm": 7.0} for c in sz_symbols]
    # BJ is missing — segment gate must catch it

    source = BatchSource("source", sh_records + sz_records)
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=all_symbols,
            config={"min_coverage_ratio": 0.80},  # 11/12 = 91.7% > 80% overall
        )
    )

    assert result.status == "failed"
    assert result.records_written == 0
    assert repo.valuation.writes == []
    sc = result.metadata["segment_coverage"]
    assert sc["BJ"]["expected"] == 1
    assert sc["BJ"]["covered"] == 0
    assert sc["BJ"]["coverage_ratio"] == 0.0
    assert "BJ" in result.metadata["failed_segments"]
    assert "segment BJ" in result.errors[0]


def test_per_segment_coverage_success(monkeypatch):
    """All segments meet their thresholds — pipeline succeeds."""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    source = BatchSource(
        "source",
        [
            {"code": "600000", "date": "20260728", "pe_ttm": 6.0},
            {"code": "000001", "date": "20260728", "pe_ttm": 7.0},
            {"code": "920002", "date": "20260728", "pe_ttm": 8.0},
        ],
    )
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=["600000", "000001", "920002"],
            config={"min_coverage_ratio": 1.0},
        )
    )

    assert result.status == "success"
    assert result.records_written == 3
    sc = result.metadata["segment_coverage"]
    assert sc["SH"]["coverage_ratio"] == 1.0
    assert sc["SZ"]["coverage_ratio"] == 1.0
    assert sc["BJ"]["coverage_ratio"] == 1.0
    assert result.metadata["failed_segments"] == []


def test_unknown_segment_failure(monkeypatch):
    """UNKNOWN expected records default to 1.0 threshold — missing UNKNOWN fails."""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    sh_symbols = [f"600{i:03d}" for i in range(10)]
    sz_symbols = [f"000{i:03d}" for i in range(10)]
    unknown_symbols = ["ABC123"]
    all_symbols = sh_symbols + sz_symbols + unknown_symbols

    sh_records = [{"code": c, "date": "20260728", "pe_ttm": 6.0} for c in sh_symbols]
    sz_records = [{"code": c, "date": "20260728", "pe_ttm": 7.0} for c in sz_symbols]
    # UNKNOWN symbol is not provided by source

    source = BatchSource("source", sh_records + sz_records)
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=all_symbols,
            config={"min_coverage_ratio": 0.80},  # 20/21 = 95.2% overall passes
        )
    )

    assert result.status == "failed"
    assert result.records_written == 0
    assert "UNKNOWN" in result.metadata["failed_segments"]
    sc = result.metadata["segment_coverage"]
    assert sc["UNKNOWN"]["expected"] == 1
    assert sc["UNKNOWN"]["covered"] == 0
    assert sc["UNKNOWN"]["min_coverage_ratio"] == 1.0
    assert "segment UNKNOWN" in result.errors[0]


def test_numeric_segment_threshold_applies_to_all_segments(monkeypatch):
    """An explicit numeric threshold applies uniformly, including UNKNOWN."""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    sh_symbols = [f"600{i:03d}" for i in range(10)]
    unknown_symbol = "ABC123"
    records = [
        {"code": code, "date": "20260728", "pe_ttm": 6.0}
        for code in sh_symbols
    ]
    source = BatchSource("source", records)
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=sh_symbols + [unknown_symbol],
            config={
                "min_coverage_ratio": 0.90,
                "segment_min_coverage_ratio": 0.0,
            },
        )
    )

    assert result.status == "success"
    assert result.records_written == 10
    sc = result.metadata["segment_coverage"]
    assert sc["SH"]["min_coverage_ratio"] == 0.0
    assert sc["UNKNOWN"]["coverage_ratio"] == 0.0
    assert sc["UNKNOWN"]["min_coverage_ratio"] == 0.0
    assert result.metadata["failed_segments"] == []


def test_dict_segment_threshold_uses_per_market_values(monkeypatch):
    """A threshold dict can relax one market without weakening the others."""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    sh_symbols = [f"600{i:03d}" for i in range(10)]
    bj_symbol = "920002"
    records = [
        {"code": code, "date": "20260728", "pe_ttm": 6.0}
        for code in sh_symbols
    ]
    source = BatchSource("source", records)
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=sh_symbols + [bj_symbol],
            config={
                "min_coverage_ratio": 0.90,
                "segment_min_coverage_ratio": {
                    "SH": 1.0,
                    "SZ": 0.95,
                    "BJ": 0.0,
                    "UNKNOWN": 1.0,
                },
            },
        )
    )

    assert result.status == "success"
    assert result.records_written == 10
    sc = result.metadata["segment_coverage"]
    assert sc["SH"]["min_coverage_ratio"] == 1.0
    assert sc["BJ"]["coverage_ratio"] == 0.0
    assert sc["BJ"]["min_coverage_ratio"] == 0.0
    assert result.metadata["failed_segments"] == []


def test_dry_run_segment_gate_zero_write(monkeypatch):
    """dry_run=True: segment gate validates but records_written=0."""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    source = BatchSource(
        "source",
        [{"code": "600000", "date": "20260728", "pe_ttm": 6.0}],
    )
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=["600000"],
            dry_run=True,
            config={"min_coverage_ratio": 1.0},
        )
    )

    assert result.status == "success"
    assert result.records_read == 1
    assert result.records_written == 0
    assert repo.valuation.writes == []
    sc = result.metadata["segment_coverage"]
    assert sc["SH"]["expected"] == 1
    assert sc["SH"]["covered"] == 1
    assert sc["SH"]["min_coverage_ratio"] == 1.0
    assert result.metadata["failed_segments"] == []


def test_none_preserved_from_source_to_repo(monkeypatch):
    """None in source record stays None through normalize into the repo write."""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    source = BatchSource(
        "source",
        [
            {
                "code": "600000",
                "date": "20260728",
                "pe_ttm": 6.0,
                "pb_mrq": None,
                "ps_ttm": None,
                "market_cap": None,
                "total_shares": None,
                "dividend_yield": None,
            }
        ],
    )
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=["600000"],
            config={"min_coverage_ratio": 1.0},
        )
    )

    assert result.status == "success"
    written = repo.valuation.writes[0][0]
    assert written["pe_ttm"] == 6.0
    assert written["pb_mrq"] is None
    assert written["ps_ttm"] is None
    assert written["market_cap"] is None
    assert written["total_shares"] is None
    assert written["dividend_yield"] is None


def test_unavailable_fields_not_converted_to_zero(monkeypatch):
    """Absent optional keys must not become 0 on the way to the repo."""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    source = BatchSource(
        "source",
        [{"code": "600000", "date": "20260728", "pe_ttm": 6.0}],
    )
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=["600000"],
            config={"min_coverage_ratio": 1.0},
        )
    )

    assert result.status == "success"
    written = repo.valuation.writes[0][0]
    assert written["market_cap"] is None
    assert written["total_shares"] is None
    assert written["dividend_yield"] is None


def test_pe_only_record_with_none_peers_is_valid(monkeypatch):
    """A record with PE and None everywhere else is a valid coverage record."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [{"code": "600000", "date": "20260728", "pe_ttm": 6.0}],
        )],
        ["600000"],
        config={"min_coverage_ratio": 1.0},
    )
    assert result.status == "success"
    assert result.records_written == 1
    assert repo.valuation.writes[0][0]["pe_ttm"] == 6.0


def test_market_cap_only_record_is_valid(monkeypatch):
    """A record with market_cap and no PE/PB/PS is a valid coverage record."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [{"code": "600000", "date": "20260728", "market_cap": 1.5e11}],
        )],
        ["600000"],
        config={"min_coverage_ratio": 1.0},
    )
    assert result.status == "success"
    written = repo.valuation.writes[0][0]
    assert written["market_cap"] == 1.5e11
    assert written["pe_ttm"] is None
    assert written["pb_mrq"] is None
    assert written["ps_ttm"] is None


def test_all_core_none_record_invalid(monkeypatch):
    """All four core fields None → record invalid → no coverage, zero write."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [{
                "code": "600000",
                "date": "20260728",
                "pe_ttm": None,
                "pb_mrq": None,
                "ps_ttm": None,
                "market_cap": None,
            }],
        )],
        ["600000"],
        config={"min_coverage_ratio": 1.0},
    )
    assert result.status == "failed"
    assert result.records_written == 0
    assert result.metadata["coverage_ratio"] == 0.0
    assert repo.valuation.writes == []


def test_all_core_zero_record_invalid(monkeypatch):
    """All four core fields 0.0 → record invalid → no coverage, zero write."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [{
                "code": "600000",
                "date": "20260728",
                "pe_ttm": 0.0,
                "pb_mrq": 0.0,
                "ps_ttm": 0.0,
                "market_cap": 0.0,
            }],
        )],
        ["600000"],
        config={"min_coverage_ratio": 1.0},
    )
    assert result.status == "failed"
    assert result.records_written == 0
    assert result.metadata["coverage_ratio"] == 0.0
    assert repo.valuation.writes == []


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_nan_inf_record_invalid(monkeypatch, bad_value):
    """NaN/Inf in any present field invalidates the record."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [{"code": "600000", "date": "20260728", "pe_ttm": bad_value}],
        )],
        ["600000"],
        config={"min_coverage_ratio": 1.0},
    )
    assert result.status == "failed"
    assert result.records_written == 0
    assert repo.valuation.writes == []


def test_non_numeric_string_invalid(monkeypatch):
    """A non-numeric string value invalidates the record."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [{"code": "600000", "date": "20260728", "pe_ttm": "not-a-number"}],
        )],
        ["600000"],
        config={"min_coverage_ratio": 1.0},
    )
    assert result.status == "failed"
    assert result.records_written == 0
    assert repo.valuation.writes == []


def test_invalid_secondary_field_invalidates_record(monkeypatch):
    """A present but non-finite supplementary field invalidates the record."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [{
                "code": "600000",
                "date": "20260728",
                "pe_ttm": 6.0,
                "total_shares": "oops",
            }],
        )],
        ["600000"],
        config={"min_coverage_ratio": 1.0},
    )
    assert result.status == "failed"
    assert result.records_written == 0
    assert repo.valuation.writes == []


def test_invalid_record_not_counted_in_overall_coverage(monkeypatch):
    """Invalid records must not inflate overall coverage."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [
                {"code": "600000", "date": "20260728", "pe_ttm": 6.0},
                {
                    "code": "000001",
                    "date": "20260728",
                    "pe_ttm": None,
                    "pb_mrq": None,
                    "ps_ttm": None,
                    "market_cap": None,
                },
            ],
        )],
        ["600000", "000001"],
        config={"min_coverage_ratio": 1.0},
    )
    assert result.status == "failed"
    assert result.metadata["coverage_ratio"] == 0.5
    assert result.records_written == 0
    assert repo.valuation.writes == []


def test_invalid_record_not_counted_in_segment_coverage(monkeypatch):
    """Invalid records must not inflate per-segment coverage."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [
                {"code": "600000", "date": "20260728", "pe_ttm": 6.0},
                {"code": "000001", "date": "20260728", "pe_ttm": None},
            ],
        )],
        ["600000", "000001"],
        config={"min_coverage_ratio": 0.5},
    )
    assert result.status == "failed"
    assert result.metadata["coverage_ratio"] == 0.5
    assert "SZ" in result.metadata["failed_segments"]
    sc = result.metadata["segment_coverage"]
    assert sc["SZ"]["covered"] == 0
    assert result.records_written == 0
    assert repo.valuation.writes == []


def test_pb_alias_used_when_pb_mrq_missing(monkeypatch):
    """pb remains a compatible alias for pb_mrq."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [{"code": "600000", "date": "20260728", "pe_ttm": 6.0, "pb": 4.5}],
        )],
        ["600000"],
        config={"min_coverage_ratio": 1.0},
    )
    assert result.status == "success"
    assert repo.valuation.writes[0][0]["pb_mrq"] == 4.5


def test_missing_pb_alias_does_not_make_zero(monkeypatch):
    """Missing pb_mrq and pb stays None, never 0."""
    repo, result = run_pipeline(
        monkeypatch,
        [BatchSource(
            "source",
            [{"code": "600000", "date": "20260728", "pe_ttm": 6.0}],
        )],
        ["600000"],
        config={"min_coverage_ratio": 1.0},
    )
    assert result.status == "success"
    assert repo.valuation.writes[0][0]["pb_mrq"] is None


def test_source_exception_without_record_failure_does_not_crash(monkeypatch):
    """Registry without record_failure: source failure is caught and gate runs."""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    source = ExplodingSource("exploding")
    registry = make_registry(source)

    result = ValuationPipeline(registry).run(
        PipelineInput(
            date="2026-07-28",
            symbols=["600000"],
            config={"min_coverage_ratio": 1.0},
        )
    )

    assert result.status == "failed"
    assert result.records_written == 0
    assert result.metadata["coverage_ratio"] == 0.0
    assert repo.valuation.writes == []

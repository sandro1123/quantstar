"""Tests for Pipeline base classes"""
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult


class TestPipelineInput:
    def test_effective_start_defaults_to_date(self):
        inp = PipelineInput(date="2026-07-24")
        assert inp.effective_start == "2026-07-24"

    def test_effective_start_uses_start_date(self):
        inp = PipelineInput(date="2026-07-24", start_date="2026-07-01")
        assert inp.effective_start == "2026-07-01"

    def test_effective_end_defaults_to_date(self):
        inp = PipelineInput(date="2026-07-24")
        assert inp.effective_end == "2026-07-24"

    def test_effective_end_uses_end_date(self):
        inp = PipelineInput(date="2026-07-24", end_date="2026-07-31")
        assert inp.effective_end == "2026-07-31"


class TestPipelineResult:
    def test_is_ok_returns_true_for_success(self):
        result = PipelineResult(name="test", date="2026-07-24", status="success")
        assert result.is_ok is True

    def test_is_ok_returns_true_for_partial(self):
        result = PipelineResult(name="test", date="2026-07-24", status="partial")
        assert result.is_ok is True

    def test_is_ok_returns_false_for_failed(self):
        result = PipelineResult(name="test", date="2026-07-24", status="failed")
        assert result.is_ok is False

    def test_execute_sets_duration(self):
        class TestPipeline(BasePipeline):
            name = "test"
            def run(self, input):
                return PipelineResult(name="test", date=input.date, status="success")

        pipeline = TestPipeline()
        inp = PipelineInput(date="2026-07-24")
        result = pipeline.execute(inp)
        assert result.duration_s > 0
        assert result.status == "success"

    def test_execute_catches_exceptions(self):
        class FailingPipeline(BasePipeline):
            name = "fail"
            def run(self, input):
                raise ValueError("test error")

        pipeline = FailingPipeline()
        inp = PipelineInput(date="2026-07-24")
        result = pipeline.execute(inp)
        assert result.status == "failed"
        assert "test error" in result.errors[0]
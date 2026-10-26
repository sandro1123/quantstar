"""Tests for PipelineRegistry"""
import pytest
from data_layer.pipelines.registry import PipelineRegistry
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult


class MockPipeline(BasePipeline):
    name = "mock"
    requires = ["stock_info"]
    produces = ["daily_klines"]

    def run(self, input: PipelineInput) -> PipelineResult:
        return PipelineResult(name=self.name, date=input.date, status="success")


class TestPipelineRegistry:
    def test_register_and_get(self):
        registry = PipelineRegistry()
        pipe = MockPipeline()
        registry.register(pipe)
        assert registry.get("mock") is pipe

    def test_get_returns_none_for_unregistered(self):
        registry = PipelineRegistry()
        assert registry.get("nonexistent") is None

    def test_get_all_returns_all_pipelines(self):
        registry = PipelineRegistry()
        registry.register(MockPipeline())
        assert len(registry.get_all()) == 1

    def test_run_executes_pipeline(self):
        registry = PipelineRegistry()
        registry.register(MockPipeline())
        inp = PipelineInput(date="2026-07-24")
        result = registry.run("mock", inp)
        assert result.status == "success"

    def test_run_raises_for_unregistered(self):
        registry = PipelineRegistry()
        with pytest.raises(ValueError, match="未注册"):
            registry.run("nonexistent", PipelineInput(date="2026-07-24"))

    def test_pipeline_name_and_requirements(self):
        pipe = MockPipeline()
        assert pipe.name == "mock"
        assert "stock_info" in pipe.requires
        assert "daily_klines" in pipe.produces
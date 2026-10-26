import pytest
"""Tests for data layer pipelines (batch)"""
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult


class TestPipelineContract:
    def test_pipeline_requires_name(self):
        class BadPipe(BasePipeline):
            def run(self, input):
                return PipelineResult(name="", date="", status="success")
        p = BadPipe()
        assert p.name == ""

    def test_pipeline_execute_catches_errors(self):
        class BrokenPipe(BasePipeline):
            name = "broken"
            def run(self, input):
                raise RuntimeError("pipe failed")
        p = BrokenPipe()
        result = p.execute(PipelineInput(date="2026-07-24"))
        assert result.status == "failed"
        assert "pipe failed" in result.errors[0]

    def test_pipeline_metadata(self):
        class TestPipe(BasePipeline):
            name = "test"
            requires = ["stock_info"]
            produces = ["daily_klines"]
            def run(self, input):
                return PipelineResult(name=self.name, date=input.date, status="success")
        p = TestPipe()
        assert p.name == "test"
        assert "stock_info" in p.requires
        assert "daily_klines" in p.produces
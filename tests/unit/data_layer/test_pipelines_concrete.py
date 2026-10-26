"""Basic tests for all 9 concrete pipelines"""
import pytest
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.pipelines.registry import PipelineRegistry
from data_layer.pipelines.kline import KlinePipeline
from data_layer.pipelines.financial import FinancialPipeline
from data_layer.pipelines.valuation import ValuationPipeline
from data_layer.pipelines.moneyflow import MoneyflowPipeline
from data_layer.pipelines.fuyao import FuyaoPipeline
from data_layer.pipelines.concept import ConceptPipeline
from data_layer.pipelines.lhb import LHBPipeline
from data_layer.pipelines.sentiment import SentimentPipeline
from data_layer.pipelines.analyst_forecast import AnalystForecastPipeline


ALL_PIPELINES = [
    KlinePipeline(),
    FinancialPipeline(),
    ValuationPipeline(),
    MoneyflowPipeline(),
    FuyaoPipeline(),
    ConceptPipeline(),
    LHBPipeline(),
    SentimentPipeline(),
    AnalystForecastPipeline(),
]


def test_nine_pipelines_imported():
    assert len(ALL_PIPELINES) == 9


def test_pipeline_names_are_unique():
    names = [p.name for p in ALL_PIPELINES]
    assert len(names) == len(set(names)), f"Duplicate pipeline names: {names}"


def test_pipeline_requires_produces():
    for pipeline in ALL_PIPELINES:
        assert isinstance(pipeline.requires, list), f"{pipeline.name}.requires not a list"
        assert isinstance(pipeline.produces, list), f"{pipeline.name}.produces not a list"
        assert pipeline.name, f"Pipeline {type(pipeline).__name__} has empty name"


def test_pipeline_has_run_method():
    for pipeline in ALL_PIPELINES:
        assert hasattr(pipeline, "run"), f"{pipeline.name} missing run()"
        assert callable(pipeline.run), f"{pipeline.name}.run() not callable"


def test_pipeline_registry_register_all():
    registry = PipelineRegistry()
    for pipeline in ALL_PIPELINES:
        registry.register(pipeline)
    assert len(registry.get_all()) == 9


def test_pipeline_registry_roundtrip():
    registry = PipelineRegistry()
    for pipeline in ALL_PIPELINES:
        registry.register(pipeline)
    for pipeline in ALL_PIPELINES:
        retrieved = registry.get(pipeline.name)
        assert retrieved is pipeline, f"{pipeline.name} roundtrip failed"


def test_pipeline_execute_fails_without_run():
    class BadPipeline(BasePipeline):
        name = "bad"

        def run(self, input: PipelineInput) -> PipelineResult:
            raise NotImplementedError("not implemented")

    registry = PipelineRegistry()
    registry.register(BadPipeline())
    result = registry.run("bad", PipelineInput(date="2026-07-17"))
    assert result.status == "failed"
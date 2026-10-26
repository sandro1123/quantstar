"""PipelineRunner 降级策略单元测试"""
import pytest
from strategy_layer.pipeline.runner import PipelineRunner, PipelineStage, PipelineRunResult


def ok_fn(x):
    return (x or 0) + 1


def fail_fn(x):
    raise ValueError("stage failed")


def ok_fallback(x):
    return 999


def test_abort_default():
    runner = PipelineRunner("test_abort")
    runner.add_stage("s1", ok_fn).add_stage("s2", fail_fn).add_stage("s3", ok_fn)
    result = runner.run(0)
    assert result.overall_status == "failed"
    assert len(result.stages) == 2
    assert result.stages[0].status == "success"
    assert result.stages[1].status == "failed"
    assert result.skipped_count == 0


def test_skip_continues():
    runner = PipelineRunner("test_skip")
    runner.add_stage("s1", ok_fn, degradation="skip")
    runner.add_stage("s2", fail_fn, degradation="skip")
    runner.add_stage("s3", ok_fn, degradation="skip")
    result = runner.run(0)
    assert result.overall_status == "success"
    assert len(result.stages) == 3
    assert result.stages[0].status == "success"
    assert result.stages[1].status == "skipped"
    assert result.stages[2].status == "success"
    assert result.skipped_count == 1


def test_retry_success_after_failure():
    call_count = [0]

    def flaky_fn(x):
        call_count[0] += 1
        if call_count[0] < 2:
            raise ValueError("retryable")
        return 42

    runner = PipelineRunner("test_retry")
    runner.add_stage("s1", flaky_fn, degradation="retry")
    result = runner.run(0)
    assert result.overall_status == "success"
    assert result.stages[0].status == "success"
    assert result.stages[0].output == 42
    assert call_count[0] == 2


def test_retry_exhausted():
    call_count = [0]

    def always_fail(x):
        call_count[0] += 1
        raise ValueError("always fail")

    runner = PipelineRunner("test_retry_exhausted")
    runner.add_stage("s1", always_fail, degradation="retry")
    result = runner.run(0)
    assert result.overall_status == "failed"
    assert result.stages[0].status == "failed"
    assert call_count[0] == 2


def test_fallback_on_failure():
    runner = PipelineRunner("test_fallback")
    runner.add_stage("s1", fail_fn, degradation="fallback", fallback_fn=ok_fallback)
    result = runner.run(0)
    assert result.overall_status == "success"
    assert result.stages[0].status == "success"
    assert result.stages[0].output == 999


def test_fallback_not_called_on_success():
    fallback_called = [False]

    def never_called(x):
        fallback_called[0] = True

    runner = PipelineRunner("test_fallback_noop")
    runner.add_stage("s1", ok_fn, degradation="fallback", fallback_fn=never_called)
    result = runner.run(0)
    assert result.overall_status == "success"
    assert result.stages[0].output == 1
    assert not fallback_called[0]


def test_skip_without_break():
    runner = PipelineRunner("test_skip_multi")
    for i in range(5):
        runner.add_stage(f"s{i}", fail_fn, degradation="skip")
    result = runner.run(0)
    assert result.overall_status == "success"
    assert len(result.stages) == 5
    assert all(s.status == "skipped" for s in result.stages)
    assert result.skipped_count == 5
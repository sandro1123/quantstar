"""PipelineRunner — 阶段化管道执行

实现状态 (ST-04, 2026-08-18): **未启用** — 仅单元测试引用, 生产零调用。
启用条件: ST-14 (run_daily 拆分为阶段函数) 完成后, 阶段对象成为编排对象再接线;
设计中的 pipeline/cache.py、pipeline/degrade.py 本就未建 (排队项)。
依据: 决策账本 S-01/S-10; 三态律之"排队"态。
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from common.logging import get_logger
from common.retry import RetryPolicy, retry_with_backoff

logger = get_logger("strategy.pipeline")


@dataclass
class PipelineStage:
    name: str
    fn: Callable[[Any], Any]
    timeout_s: float = 30.0
    degradation: str = "abort"
    fallback_fn: Callable[[Any], Any] | None = None


@dataclass
class PipelineResult:
    name: str
    status: str  # "success" | "failed" | "skipped"
    output: Any = None
    error: str = ""
    duration_ms: float = 0.0


@dataclass
class PipelineRunResult:
    stages: list[PipelineResult] = field(default_factory=list)
    overall_status: str = "success"
    skipped_count: int = 0


class PipelineRunner:
    def __init__(self, name: str = "default"):
        self.name = name
        self.stages: list[PipelineStage] = []

    def add_stage(self, name: str, fn: Callable, timeout_s: float = 30.0,
                  degradation: str = "abort", fallback_fn: Callable[[Any], Any] | None = None):
        self.stages.append(PipelineStage(name=name, fn=fn, timeout_s=timeout_s,
                                         degradation=degradation, fallback_fn=fallback_fn))
        return self

    def run(self, initial_input: Any = None) -> PipelineRunResult:
        import time
        results = []
        current_input = initial_input
        skipped_count = 0
        for stage in self.stages:
            start = time.time()
            try:
                if stage.degradation == "retry":
                    policy = RetryPolicy(max_retries=2, backoff_base=2.0, retryable_exceptions=(Exception,))
                    output = retry_with_backoff(stage.fn, policy, current_input)
                elif stage.degradation == "fallback" and stage.fallback_fn is not None:
                    try:
                        output = stage.fn(current_input)
                    except Exception as e:
                        logger.warning(f"Pipeline {self.name} stage {stage.name} failed, using fallback: {e}")
                        output = stage.fallback_fn(current_input)
                else:
                    output = stage.fn(current_input)
                elapsed = (time.time() - start) * 1000
                results.append(PipelineResult(name=stage.name, status="success", output=output, duration_ms=elapsed))
                current_input = output
            except Exception as e:
                elapsed = (time.time() - start) * 1000
                if stage.degradation == "skip":
                    results.append(PipelineResult(name=stage.name, status="skipped", error=str(e), duration_ms=elapsed))
                    logger.warning(f"Pipeline {self.name} stage {stage.name} skipped: {e}")
                    skipped_count += 1
                    continue
                results.append(PipelineResult(name=stage.name, status="failed", error=str(e), duration_ms=elapsed))
                logger.warning(f"Pipeline {self.name} stage {stage.name} failed: {e}")
                break
        overall = "failed" if any(r.status == "failed" for r in results) else "success"
        logger.info(f"Pipeline {self.name}: {overall} ({len(results)} stages, {skipped_count} skipped)")
        return PipelineRunResult(stages=results, overall_status=overall, skipped_count=skipped_count)

"""件2 concept_membership 区间通电接线测试 (统筹 sm-8683e2dff09a4717 件2,
照 R4 concept_plates_daily 先例 — TDD 同D纪律)。

约束 (统筹四点):
  1. dispatch 表挂 concept_intervals_daily → _run_concept_intervals_daily;
  2. GO-7 硬闸语义保持: 不进 DAILY_SUB_PIPELINES (双名钉);
  3. ops.yaml 管道定义四要素 (schedule/days/timeout_s/depends_on);
  4. handler config backfill=False + sleep_s 显式 (回补腿另窗)。

窗口: 17:05 — 在 16:35 concept_plates_daily 写完 (~16:40 净退) 之后,
16:52 agent_evaluate 之前错峰; zzshare 适配器同源分摊限流面, 两管道
间 30min 隔离。
"""
import re
from pathlib import Path

import pytest

from ops_layer.orchestrator import DAILY_SUB_PIPELINES

_root = Path(__file__).resolve().parents[3]


def _read(p):
    return p.read_text(encoding="utf-8")


def test_dispatch_wired():
    """orchestrator dispatch 挂 concept_intervals_daily。"""
    orch = _read(_root / "src" / "ops_layer" / "orchestrator.py")
    assert '"concept_intervals_daily": self._run_concept_intervals_daily' in orch
    assert "def _run_concept_intervals_daily" in orch


def test_go7_semantics_daily_sub_pipelines_untouched():
    """GO-7 硬闸语义保持: 日管道子清单不含 concept_intervals 两名。"""
    subs = " ".join(DAILY_SUB_PIPELINES)
    assert "concept_intervals" not in subs, "concept_intervals 不得进 DAILY_SUB_PIPELINES (GO-7)"
    assert "concept_intervals_daily" not in subs, "顶层小管道也不进日管道 (独立排程)"


def test_ops_yaml_pipeline_defined():
    """排程准绳: config/defaults/ops.yaml 管道定义四要素。"""
    y = _read(_root / "config" / "defaults" / "ops.yaml")
    m = re.search(r"^    concept_intervals_daily:\n((?:      .*\n)+)", y, re.M)
    assert m, "ops.yaml 缺 concept_intervals_daily 管道定义"
    body = m.group(1)
    assert 'schedule: "17:05"' in body, "窗=17:05 (16:35 真概念写完 30min 隔离后)"
    assert 'days: "1-5"' in body, "交易日 1-5"
    assert "timeout_s: 900" in body, "超时 900s (区间拉取一次得全史, fetch 较慢)"
    assert "depends_on: []" in body, "无依赖 (独立错峰小管道)"


def test_handler_config_daily_not_backfill():
    """handler config backfill=False (每日腿; 回补腿另窗统筹裁)。"""
    orch = _read(_root / "src" / "ops_layer" / "orchestrator.py")
    m = re.search(r"def _run_concept_intervals_daily.*?config=\{(.*?)\}",
                  orch, re.S)
    assert m, "handler 未找到 PipelineInput config"
    assert '"backfill": False' in m.group(1), "每日通电必须 backfill=False"


def test_registry_registered():
    """ConceptIntervalsPipeline 已注册 PipelineRegistry (调研卡实证: 已在,
    本测试防回归 — 建了没通电案的反面钉)。"""
    src = _read(_root / "src" / "data_layer" / "__init__.py")
    assert "ConceptIntervalsPipeline(source_registry=" in src
    assert re.search(r'pipelines\.register\(ConceptIntervalsPipeline', src)

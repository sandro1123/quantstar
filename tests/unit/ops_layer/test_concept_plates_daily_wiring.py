"""R4-② concept_plates_daily 顶层小管道接线测试
(统筹 sm-39c8b41402a748ee ①②; 施工规范沿用 dl51_wiring 同款)。

约束 (统筹裁定主案语义核心):
  1. dispatch 表挂 concept_plates_daily → _run_concept_plates_daily;
  2. GO-7 硬闸语义保持: 独立顶层管道 ≠ DAILY_SUB_PIPELINES 成员 —
     concept_plates 仍不得出现在日管道子清单 (test_dl49 已钉, 本文件加
     顶层名与子管道名的双重确认);
  3. ops.yaml 管道定义在位 (排程准绳): schedule 16:35 / days 1-5 /
     timeout_s 600 / depends_on 空。
"""
import re
from pathlib import Path

import pytest

from ops_layer.orchestrator import DAILY_SUB_PIPELINES

_root = Path(__file__).resolve().parents[3]


def _read(p):
    return p.read_text(encoding="utf-8")


def test_dispatch_wired():
    """orchestrator dispatch 挂 concept_plates_daily。"""
    orch = _read(_root / "src" / "ops_layer" / "orchestrator.py")
    assert '"concept_plates_daily": self._run_concept_plates_daily' in orch
    assert "def _run_concept_plates_daily" in orch


def test_go7_semantics_daily_sub_pipelines_untouched():
    """GO-7 硬闸语义保持: 日管道子清单不含 concept_plates / concept_plates_daily。

    独立顶层管道 (排程+dispatch) 与「接进日管道」是两条不同的路 —
    主案选的是前者, 日管道成员面必须零变化 (统筹「DDP 零改动」)。
    """
    subs = " ".join(DAILY_SUB_PIPELINES)
    assert "concept_plates" not in subs, "concept_plates 不得进 DAILY_SUB_PIPELINES (GO-7)"
    assert "concept_plates_daily" not in subs, "顶层小管道也不进日管道 (它是独立排程)"


def test_ops_yaml_pipeline_defined():
    """排程准绳: config/defaults/ops.yaml 管道定义四要素。"""
    y = _read(_root / "config" / "defaults" / "ops.yaml")
    m = re.search(r"^    concept_plates_daily:\n((?:      .*\n)+)", y, re.M)
    assert m, "ops.yaml 缺 concept_plates_daily 管道定义"
    body = m.group(1)
    assert 'schedule: "16:35"' in body, "窗=16:35 (统筹裁定)"
    assert 'days: "1-5"' in body, "交易日 1-5"
    assert "timeout_s: 600" in body, "超时 600s (fetch 142s+写锁 30s+余量)"
    assert "depends_on: []" in body, "无依赖 (独立错峰小管道)"


def test_handler_config_daily_not_backfill():
    """handler config 必须是 backfill=False (每日自然积累段, 与回补段可区分 —
    统筹裁定: 定权重只用无偏段, 回补/每日必须可区分)。"""
    orch = _read(_root / "src" / "ops_layer" / "orchestrator.py")
    m = re.search(r"def _run_concept_plates_daily.*?config=\{(.*?)\}",
                  orch, re.S)
    assert m, "handler 未找到 PipelineInput config"
    assert '"backfill": False' in m.group(1), "每日通电必须 backfill=False"

# -*- coding: utf-8 -*-
"""P1-4 bj_legacy_skipped 计数进 metadata (09-28)。

缺陷 (09-25 塌方侦察): kline 腿「跳过 250 只北交所旧码」只有 logger.info,
PipelineResult.metadata 无此计数 — pipeline_runs 看不到「实际处理 5300 还是
5550」, 覆盖率分母不可审计 (省掉时间限定的计数=没出处的数 同族)。

修法: kline.py run() skipped 计数写 metadata['bj_legacy_skipped']。
"""
from unittest.mock import MagicMock, patch


def _run_kline(symbols):
    """跑 KlinePipeline.run 到 metadata, mock 掉取数与写库。"""
    from data_layer.pipelines.kline import KlinePipeline
    from data_layer.pipelines.base import PipelineInput

    registry = MagicMock()
    registry.get_sources_for.return_value = []  # 无源 → 逐股/bulk 都空跑
    pipe = KlinePipeline(source_registry=registry)

    repo = MagicMock()
    repo.stock.get_all_active.return_value = symbols
    inp = PipelineInput(date="2026-09-25", db=MagicMock(), symbols=None,
                        start_date="2026-09-25", end_date="2026-09-25")
    with patch("data_layer.pipelines.kline.get_repo", return_value=repo):
        return pipe.run(inp)


# 钉①: 含 43/83/87 前缀 → metadata 计数正确
def test_bj_legacy_skipped_counted():
    symbols = ["600519", "430001", "830002", "870003", "000001"]
    result = _run_kline(symbols)
    md = result.metadata or {}
    assert md.get("bj_legacy_skipped") == 3, \
        f"3 只北交所旧码 (43/83/87) 应计数: {md}"


# 钉②: 无旧码 → 键存在且为 0 (不缺席 — 缺席=盲)
def test_bj_legacy_skipped_zero_present():
    result = _run_kline(["600519", "000001"])
    md = result.metadata or {}
    assert "bj_legacy_skipped" in md, "键须存在 (缺席=盲, 违反不静默)"
    assert md["bj_legacy_skipped"] == 0

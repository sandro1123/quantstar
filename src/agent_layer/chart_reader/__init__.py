# -*- coding: utf-8 -*-
"""chart_reader — 09 图形读图答题工人 (#101 LLM读图立即接入)

Leg A 规则腿 (切1, 全确定性): pivot 分形 / level 聚类 / 量能结构 + chart_analysis 落库。
Leg B LLM腿 (切2): 60日OHLCV 结构化摘要 → schema 强校验 → ATR 护栏 → 双录落库。

层间契约 (§1.5): 只写自家结论表 chart_analysis (表即跨层界面),
永不写 signal / pending_entry (唯一写者 = 02); LLM 结论永不进打分公式。
"""
from agent_layer.chart_reader.levels import (
    Level,
    LevelClusterer,
    Pivot,
    PivotDetector,
    VolumeAnalyzer,
    build_levels,
)
from agent_layer.chart_reader.repository import ChartAnalysisRepository, atr_band
from agent_layer.chart_reader.leg_b import (
    ChartConclusion,
    ChartLLMAnalyzer,
    build_ohlcv_summary,
    compute_atr,
    load_candidates,
    validate_conclusion,
)
from agent_layer.chart_reader.evaluation import (
    EVAL_HORIZON_BARS,
    compare_sources,
)

__all__ = [
    "Pivot",
    "Level",
    "PivotDetector",
    "LevelClusterer",
    "VolumeAnalyzer",
    "build_levels",
    "ChartAnalysisRepository",
    "atr_band",
    "ChartConclusion",
    "ChartLLMAnalyzer",
    "build_ohlcv_summary",
    "compute_atr",
    "load_candidates",
    "validate_conclusion",
    "EVAL_HORIZON_BARS",
    "compare_sources",
]

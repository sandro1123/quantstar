# -*- coding: utf-8 -*-
"""报告层二次加工子包 — 推荐逻辑贯通卡 模块1b"""
from .models import RecommendationArgument, AdvisorSentence, AdvisorResult, SOURCE_WHITELIST
from .fidelity import FidelityGuard, GuardVerdict, DegradedOutput
from .source import load_argument
from .rewrite import rewrite_to_advisor, translate_terms, PENDING_LINE, RULE_NOTE
from .disclaimer import DISCLAIMER
from .guard_trace import (ensure_guard_trace_table, record_guard_rejection,
                          GATE_CONCLUSION, GATE_EVIDENCE, GATE_TRACE,
                          PATH_RULE_FALLBACK, PATH_RAW)
from .factor_extract import extract_factors, register_extractor
from .rationale_join import (ensure_rationale_table, load_argument_from_rationale,
                             write_back_advisor, find_orphan_traces,
                             GUARD_PASS, GUARD_DEGRADED, GUARD_REJECTED)
from .trace_conn import resolve_trace_conn
from .persist import persist_advisor

__all__ = [
    "RecommendationArgument", "AdvisorSentence", "AdvisorResult", "SOURCE_WHITELIST",
    "FidelityGuard", "GuardVerdict", "DegradedOutput",
    "load_argument", "rewrite_to_advisor", "translate_terms", "PENDING_LINE", "RULE_NOTE",
    "DISCLAIMER",
    "ensure_guard_trace_table", "record_guard_rejection",
    "GATE_CONCLUSION", "GATE_EVIDENCE", "GATE_TRACE", "PATH_RULE_FALLBACK", "PATH_RAW",
    "extract_factors", "register_extractor",
    "ensure_rationale_table", "load_argument_from_rationale", "write_back_advisor",
    "find_orphan_traces", "GUARD_PASS", "GUARD_DEGRADED", "GUARD_REJECTED",
    "resolve_trace_conn", "persist_advisor",
]

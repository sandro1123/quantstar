# -*- coding: utf-8 -*-
"""件② 因子面接口收口 TDD — extract_factors 单一入口，可替换实现

现在 factors=从 reasoning 文本提取（过渡态）；1a schema 落结构化因子列表后
换实现不动调用方（统筹裁定：只做到接口预留，不提前写死 schema 字段）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from report_layer.advisor.factor_extract import extract_factors, register_extractor


class TestExtractFactors:
    def test_default_extracts_from_text(self):
        """当前实现：文本提取（'动量因子近20日强势'→['动量']）"""
        arg = type("A", (), {"reasoning": "动量因子近20日强势，换手率因子异常", "factors": None})()
        assert extract_factors(arg) == ["动量", "换手率"]

    def test_uses_structured_field_when_present(self):
        """原料自带结构化 factors 字段 → 直接用（1a 落表后的常态路径）"""
        arg = type("A", (), {"reasoning": "动量因子近20日强势", "factors": ["momentum_strength"]})()
        assert extract_factors(arg) == ["momentum_strength"]

    def test_empty_reasoning_safe(self):
        arg = type("A", (), {"reasoning": "", "factors": None})()
        assert extract_factors(arg) == []

    def test_none_argument_safe(self):
        assert extract_factors(None) == []

    def test_custom_extractor_registration(self):
        """可替换实现：register_extractor 后走新提取器（1a schema 到位时的换法）"""
        arg = type("A", (), {"reasoning": "x", "factors": None})()

        def fake_extractor(a):
            return ["structured_factor_a"]

        register_extractor(fake_extractor)
        try:
            assert extract_factors(arg) == ["structured_factor_a"]
        finally:
            register_extractor(None)  # 还原默认

    def test_extractor_exception_falls_back_to_text(self):
        """自定义提取器抛错 → 回落文本提取（不炸主链，留 warn）"""
        arg = type("A", (), {"reasoning": "动量因子强势", "factors": None})()

        def bad_extractor(a):
            raise RuntimeError("schema 还没接通")

        register_extractor(bad_extractor)
        try:
            assert extract_factors(arg) == ["动量"]
        finally:
            register_extractor(None)

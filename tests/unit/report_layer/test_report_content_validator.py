# -*- coding: utf-8 -*-
"""内容校验器 — 卡片形态豁免 sections 空判 (校验面跟上格式演进)。"""
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root / "src"))

from report_layer.validator import ContentValidator  # noqa: E402


def test_card_shape_not_flagged_empty():
    """卡片形态内容 (无 sections 键) → 不报「报告段落为空」。"""
    card_content = {"msg_type": "interactive",
                    "card": {"header": {"title": {"content": "x"}},
                             "elements": [{"tag": "div"}]}}
    issues = ContentValidator().validate(card_content)
    assert not any("报告段落为空" in i.message for i in issues)


def test_legacy_empty_sections_still_flagged():
    """legacy 形态 sections 真空 → 仍报 (校验语义不丢)。"""
    issues = ContentValidator().validate({"sections": []})
    assert any("报告段落为空" in i.message for i in issues)


def test_legacy_sections_checks_intact():
    """legacy 形态正常段 → 零 issue; 超长段仍抓。"""
    ok = ContentValidator().validate({"sections": [{"name": "a", "body": "文本"}]})
    assert ok == []
    long_body = {"sections": [{"name": "a", "body": "x" * 6000}]}
    issues = ContentValidator().validate(long_body)
    assert any("超长" in i.message for i in issues)

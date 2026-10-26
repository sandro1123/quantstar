"""安全层增强测试 — Guardrail"""
from agent_layer.security.guardrail import Guardrail


def test_guardrail_clean_passes():
    g = Guardrail()
    result = g.check("这是一只正常的股票分析")
    assert result.passed is True


def test_guardrail_pii_detected():
    g = Guardrail()
    result = g.check("身份证号11010119900101123X")
    assert result.passed is False
    levels = [r[0] for r in result.flagged]
    assert "pii" in levels


def test_guardrail_compliance_detected():
    g = Guardrail()
    result = g.check("我们有内幕消息")
    assert result.passed is False
    levels = [r[0] for r in result.flagged]
    assert "compliance" in levels


def test_guardrail_content_detected():
    g = Guardrail()
    result = g.check("推荐满仓买入")
    assert result.passed is False
    levels = [r[0] for r in result.flagged]
    assert "content" in levels


def test_guardrail_multiple_flags():
    g = Guardrail()
    result = g.check("内幕消息，保证收益，推荐满仓")
    assert result.passed is False
    assert len(result.flagged) >= 2
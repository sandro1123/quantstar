"""委员会重构测试 — 直接导入analyst_value"""
from agent_layer.committee.analyst_value import ValueAnalyst, MomentumAnalyst, RiskAnalyst, TechnicalAnalyst, FundamentalAnalyst, SentimentAnalyst, QuantAnalyst
from agent_layer.committee.chairman import Chairman


def test_all_analysts_importable():
    assert ValueAnalyst is not None
    assert MomentumAnalyst is not None
    assert RiskAnalyst is not None
    assert TechnicalAnalyst is not None
    assert FundamentalAnalyst is not None
    assert SentimentAnalyst is not None
    assert QuantAnalyst is not None


def test_chairman_importable():
    chairman = Chairman()
    assert chairman is not None
    assert hasattr(chairman, "analyze")


def test_skills_enhanced():
    import tempfile, os
    from agent_layer.skills.loader import SkillLoader
    with tempfile.TemporaryDirectory() as tmp:
        for name in ["stock-analysis", "risk-assessment"]:
            os.makedirs(os.path.join(tmp, name))
            with open(os.path.join(tmp, name, "SKILL.md"), "w") as f:
                f.write(f"# {name}\n\n分析框架")
        loader = SkillLoader(tmp)
        loader.register_skill("stock-analysis", priority=5)
        loader.register_skill("risk-assessment", priority=8)
        skills = loader.list_skills()
        assert "stock-analysis" in skills
        assert "risk-assessment" in skills
        content = loader.select_for_task("stock_analysis")
        assert content is not None
        assert "分析框架" in content
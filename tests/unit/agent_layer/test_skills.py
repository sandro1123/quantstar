"""Skills 层测试 — 加载 / 优先级路由"""
import os
import tempfile
from agent_layer.skills.loader import SkillLoader


def test_skill_loader_empty():
    with tempfile.TemporaryDirectory() as tmp:
        loader = SkillLoader(tmp)
        skills = loader.list_skills()
        assert skills == []


def test_skill_loader_create_skill():
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = os.path.join(tmp, "stock-analysis")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
            f.write("# Stock Analysis Skill\n\n分析框架")

        loader = SkillLoader(tmp)
        skills = loader.list_skills()
        assert "stock-analysis" in skills

        content = loader.load("stock-analysis")
        assert content is not None
        assert "分析框架" in content


def test_skill_loader_not_found():
    with tempfile.TemporaryDirectory() as tmp:
        loader = SkillLoader(tmp)
        content = loader.load("nonexistent")
        assert content is None


def test_skill_priority():
    with tempfile.TemporaryDirectory() as tmp:
        for name in ["stock-analysis", "risk-assessment"]:
            os.makedirs(os.path.join(tmp, name))
            with open(os.path.join(tmp, name, "SKILL.md"), "w") as f:
                f.write(f"# {name}")

        loader = SkillLoader(tmp)
        loader.register_skill("stock-analysis", priority=5)
        loader.register_skill("risk-assessment", priority=8)

        selected = loader.select_for_task("stock_analysis")
        assert selected is not None


def test_skill_select_for_task():
    with tempfile.TemporaryDirectory() as tmp:
        for name in ["stock-analysis", "risk-assessment"]:
            os.makedirs(os.path.join(tmp, name))
            with open(os.path.join(tmp, name, "SKILL.md"), "w") as f:
                f.write(f"# {name}")

        loader = SkillLoader(tmp)
        loader.register_skill("stock-analysis", priority=5)
        loader.register_skill("risk-assessment", priority=8)

        selected = loader.select_for_task("entry_check")
        assert selected is not None


def test_skill_should_activate():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "stock-analysis"))
        with open(os.path.join(tmp, "stock-analysis", "SKILL.md"), "w") as f:
            f.write("# test")

        loader = SkillLoader(tmp)
        assert loader.should_activate("stock_analysis")
        assert not loader.should_activate("unknown_task")
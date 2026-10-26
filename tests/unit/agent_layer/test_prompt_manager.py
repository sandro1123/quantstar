"""PromptManager + SkillLoader 单元测试"""
import pytest
import tempfile
from pathlib import Path
from agent_layer.llm.prompt_manager import PromptManager
from agent_layer.skills.loader import SkillLoader


def test_prompt_manager_load():
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "test.txt").write_text("Hello {name}, date={date}")
        pm = PromptManager(prompts_dir=tmpdir)
        result = pm.load("test", name="World", date="2026-07-17")
        assert result == "Hello World, date=2026-07-17"


def test_prompt_manager_missing():
    pm = PromptManager(prompts_dir="/nonexistent")
    assert pm.load("missing") is None


def test_prompt_manager_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "a.txt").write_text("a")
        (Path(tmpdir) / "b.txt").write_text("b")
        pm = PromptManager(prompts_dir=tmpdir)
        assert sorted(pm.list_templates()) == ["a", "b"]


def test_prompt_manager_safe_format():
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "t.txt").write_text("{{escaped}} {missing} {present}")
        pm = PromptManager(prompts_dir=tmpdir)
        result = pm.load("t", present="yes")
        assert "{missing}" in result
        assert "yes" in result


def test_skill_loader_load():
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Test Skill\nContent here")
        loader = SkillLoader(skills_dir=tmpdir)
        result = loader.load("test-skill")
        assert "Test Skill" in result


def test_skill_loader_missing():
    loader = SkillLoader(skills_dir="/nonexistent")
    assert loader.load("missing") is None


def test_skill_loader_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "skill-a").mkdir()
        (Path(tmpdir) / "skill-a" / "SKILL.md").write_text("a")
        (Path(tmpdir) / "skill-b").mkdir()
        (Path(tmpdir) / "skill-b" / "SKILL.md").write_text("b")
        (Path(tmpdir) / "no-skill").mkdir()
        loader = SkillLoader(skills_dir=tmpdir)
        assert sorted(loader.list_skills()) == ["skill-a", "skill-b"]

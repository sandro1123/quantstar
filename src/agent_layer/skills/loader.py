"""Skill Loader — SKILL.md 方法论文件加载 + 优先级路由 + YAML frontmatter"""
import re
from pathlib import Path

from common.logging import get_logger

logger = get_logger("agent.skills")

SKILLS_DIR = Path(__file__).parent
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class SkillLoader:
    def __init__(self, skills_dir: str = None):
        self._dir = Path(skills_dir) if skills_dir else SKILLS_DIR
        self._priority: dict[str, int] = {}

    def _parse_frontmatter(self, content: str) -> tuple[dict, str]:
        """解析 YAML frontmatter，返回 (metadata, body)"""
        match = FRONTMATTER_RE.match(content)
        if not match:
            return {}, content
        meta = {}
        for line in match.group(1).split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                meta[key.strip()] = val.strip().strip('"')
        return meta, content[match.end():]

    def register_skill(self, name: str, priority: int = 5):
        self._priority[name] = priority

    def load(self, skill_name: str) -> str | None:
        path = self._dir / skill_name / "SKILL.md"
        if not path.exists():
            logger.warning(f"skill not found: {path}")
            return None
        content = path.read_text(encoding="utf-8")
        meta, body = self._parse_frontmatter(content)
        if meta.get("name"):
            self.register_skill(meta["name"], int(meta.get("priority", 5)))
        return body

    def list_skills(self) -> list[str]:
        if not self._dir.exists():
            return []
        return [d.name for d in self._dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]

    def select_for_task(self, task: str) -> str | None:
        """按 task 选择技能，冲突时按优先级解决"""
        task_map = {
            "stock_analysis": ["stock-analysis", "risk-assessment"],
            "debate": ["stock-analysis", "risk-assessment"],
            "committee": ["stock-analysis", "risk-assessment", "canslim-screening"],
            "entry_check": ["risk-assessment"],
            "exit_review": ["risk-assessment", "stock-analysis"],
        }
        candidates = task_map.get(task, [])
        available = self.list_skills()
        matched = [s for s in candidates if s in available]
        if not matched:
            return None
        # 按优先级排序，选最高优先级
        matched.sort(key=lambda s: self._priority.get(s, 5), reverse=True)
        selected = matched[0]
        content = self.load(selected)
        if content:
            logger.info(f"Skill selected: {selected} (priority={self._priority.get(selected, 5)})")
        return content

    def should_activate(self, task: str) -> bool:
        skills = self.list_skills()
        return len(skills) > 0 and task in ("stock_analysis", "debate", "committee", "entry_check", "exit_review")

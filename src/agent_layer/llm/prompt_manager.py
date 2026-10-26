"""Prompt Manager — 按 agent 分类管理 prompt 模板"""
import re
from pathlib import Path

from common.logging import get_logger

logger = get_logger("agent.llm.prompts")

PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "config" / "prompts"


class PromptManager:
    def __init__(self, prompts_dir: str = None):
        self._dir = Path(prompts_dir) if prompts_dir else PROMPTS_DIR

    def load(self, template_name: str, **kwargs) -> str | None:
        path = self._dir / f"{template_name}.txt"
        if not path.exists():
            logger.warning(f"prompt template not found: {path}")
            return None
        template = path.read_text(encoding="utf-8")
        return self._safe_format(template, **kwargs)

    def _safe_format(self, template: str, **kwargs) -> str:
        def _replace(m):
            key = m.group(1)
            if key in kwargs:
                return str(kwargs[key])
            return m.group(0)
        return re.sub(r'\{(\w+)\}', _replace, template)

    def list_templates(self) -> list[str]:
        if not self._dir.exists():
            return []
        return [p.stem for p in self._dir.glob("*.txt")]

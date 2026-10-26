"""Agent Memory — 跨请求记忆"""

from agent_layer.quality.repository import AgentQualityRepository


class AgentMemory:
    def __init__(self, database=None):
        self.repo = AgentQualityRepository(database)
        self.repo.ensure_tables()

    def remember(self, code: str, date: str, verdict: str, confidence: float, reasoning: str = ""):
        self.repo.memory_remember(code, date, verdict, confidence, reasoning)

    def recall(self, code: str) -> dict | None:
        return self.repo.memory_recall(code)

    def recall_history(self, code: str, limit: int = 5) -> list[dict]:
        return self.repo.memory_recall_history(code, limit)

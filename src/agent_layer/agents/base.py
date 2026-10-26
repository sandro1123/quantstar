"""BaseAgent 抽象基类"""

from abc import ABC, abstractmethod


class BaseAgent(ABC):
    name: str = ""

    @abstractmethod
    def analyze(self, input_data: dict) -> dict: ...

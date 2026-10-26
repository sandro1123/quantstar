"""ML 评分器基类"""
from abc import ABC, abstractmethod


class BaseScorer(ABC):
    @abstractmethod
    def train(self, X: list[list[float]], y: list[float]) -> dict:
        """训练模型，返回 metrics"""
        ...

    @abstractmethod
    def predict(self, X: list[list[float]]) -> list[float]:
        """预测评分"""
        ...

    @abstractmethod
    def save(self, path: str) -> None:
        """保存模型"""
        ...

    @abstractmethod
    def load(self, path: str) -> None:
        """加载模型"""
        ...

"""Pluggable Analyzer architecture (G-E12)"""

from abc import ABC, abstractmethod

from common.database import Database


class BaseAnalyzer(ABC):
    """Base class for all analyzers"""

    def __init__(self, db: Database):
        self.db = db

    @abstractmethod
    def analyze(self) -> dict:
        """Run analysis and return results"""

    @abstractmethod
    def name(self) -> str:
        """Analyzer display name"""

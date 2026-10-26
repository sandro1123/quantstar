"""repos/__init__.py — 统一 Repository 入口"""
from common.database import Database
from data_layer.repos.financial import FinancialRepo
from data_layer.repos.kline import KlineRepo
from data_layer.repos.moneyflow import MoneyflowRepo
from data_layer.repos.stock import StockRepo
from data_layer.repos.valuation import ValuationRepo


class RepoContainer:
    def __init__(self, db: Database):
        self.kline = KlineRepo(db)
        self.stock = StockRepo(db)
        self.financial = FinancialRepo(db)
        self.valuation = ValuationRepo(db)
        self.moneyflow = MoneyflowRepo(db)


_repo_instance: RepoContainer | None = None


def get_repo(db: Database | None = None) -> RepoContainer:
    global _repo_instance
    if db is not None:
        _repo_instance = RepoContainer(db)
        return _repo_instance
    if _repo_instance is None:
        _repo_instance = RepoContainer(Database())
    return _repo_instance

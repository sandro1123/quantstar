"""Tests for data layer repositories"""
from common.database import Database
from data_layer.repos.financial import FinancialRepo
from data_layer.repos.kline import KlineRepo
from data_layer.repos.moneyflow import MoneyflowRepo


class TestKlineRepo:
    def test_batch_upsert_and_get_max_date(self):
        db = Database(":memory:")
        repo = KlineRepo(db)
        repo.batch_upsert([
            {"code": "000001", "date": "2026-07-24", "close": 10.0, "open": 9.8},
            {"code": "000002", "date": "2026-07-24", "close": 20.0, "open": 19.5},
        ])
        assert repo.get_max_date() == "2026-07-24"
        rows = repo.get_range("000001", "2026-07-24", "2026-07-24")
        assert len(rows) == 1
        assert rows[0]["close"] == 10.0

    def test_get_latest_date_returns_correct(self):
        db = Database(":memory:")
        repo = KlineRepo(db)
        repo.batch_upsert([
            {"code": "000001", "date": "2026-07-21", "close": 9.0},
            {"code": "000001", "date": "2026-07-24", "close": 10.0},
        ])
        assert repo.get_latest_date("000001") == "2026-07-24"

    def test_get_max_date_returns_none_on_empty(self):
        db = Database(":memory:")
        repo = KlineRepo(db)
        assert repo.get_max_date() is None

    def test_batch_upsert_empty_does_nothing(self):
        db = Database(":memory:")
        repo = KlineRepo(db)
        repo.batch_upsert([])
        assert repo.get_max_date() is None


class TestFinancialRepo:
    def test_batch_upsert_and_get_range(self):
        db = Database(":memory:")
        repo = FinancialRepo(db)
        repo.batch_upsert([
            {"code": "000001", "date": "2026-07-24", "eps": 0.5, "roe": 10.0},
        ])
        rows = repo.get_range("000001", "2026-07-24", "2026-07-24")
        assert len(rows) == 1
        assert rows[0]["eps"] == 0.5
        assert rows[0]["roe"] == 10.0

    def test_get_range_empty_for_missing_code(self):
        db = Database(":memory:")
        repo = FinancialRepo(db)
        repo._ensure_table()
        rows = repo.get_range("nonexistent", "2026-07-24", "2026-07-24")
        assert rows == []

    def test_codes_for_date_returns_distinct_codes(self):
        db = Database(":memory:")
        repo = FinancialRepo(db)
        repo.batch_upsert([
            {"code": "000001", "date": "2026-03-30", "eps": 0.5, "roe": 10.0},
            {"code": "000002", "date": "2026-03-30", "eps": 0.3, "roe": 8.0},
            {"code": "000003", "date": "2026-03-31", "eps": 0.2, "roe": 5.0},
        ])
        assert repo.codes_for_date("2026-03-30") == {"000001", "000002"}
        assert repo.codes_for_date("2026-03-31") == {"000003"}
        assert repo.codes_for_date("2026-12-31") == set()


class TestMoneyflowRepo:
    def test_batch_upsert_and_get_range(self):
        db = Database(":memory:")
        repo = MoneyflowRepo(db)
        repo.batch_upsert([
            {"code": "000001", "date": "2026-07-24", "main_net_inflow": 1_000_000},
        ])
        rows = repo.get_range("000001", "2026-07-24", "2026-07-24")
        assert len(rows) == 1
        assert rows[0]["main_net_inflow"] == 1_000_000

    def test_get_range_empty_for_missing_code(self):
        db = Database(":memory:")
        repo = MoneyflowRepo(db)
        repo._ensure_table()
        rows = repo.get_range("nonexistent", "2026-07-24", "2026-07-24")
        assert rows == []
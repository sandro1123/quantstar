"""Tests for KlinePipeline skip logic"""
from common.database import Database
from data_layer.repos.kline import KlineRepo


class TestKlineRepoHasValidChangePct:
    def test_has_valid_change_pct_returns_true_when_all_valid(self):
        db = Database(":memory:")
        repo = KlineRepo(db)
        conn = db.get_connection()
        repo._ensure_table()
        for i in range(5):
            conn.execute(
                "INSERT INTO daily_klines (code, date, close, change_pct) VALUES (?, ?, ?, ?)",
                (f"00000{i}", "2026-07-24", 10.0 + i, 1.0 + i),
            )
        conn.commit()
        assert repo.has_valid_change_pct("2026-07-24", "2026-07-24") is True

    def test_has_valid_change_pct_returns_false_when_all_zero(self):
        db = Database(":memory:")
        repo = KlineRepo(db)
        conn = db.get_connection()
        repo._ensure_table()
        for i in range(5):
            conn.execute(
                "INSERT INTO daily_klines (code, date, close, change_pct) VALUES (?, ?, ?, ?)",
                (f"00000{i}", "2026-07-24", 10.0 + i, 0.0),
            )
        conn.commit()
        assert repo.has_valid_change_pct("2026-07-24", "2026-07-24") is False

    def test_has_valid_change_pct_returns_false_when_below_threshold(self):
        db = Database(":memory:")
        repo = KlineRepo(db)
        conn = db.get_connection()
        repo._ensure_table()
        for i in range(10):
            change = 1.0 if i < 3 else 0.0
            conn.execute(
                "INSERT INTO daily_klines (code, date, close, change_pct) VALUES (?, ?, ?, ?)",
                (f"00000{i}", "2026-07-24", 10.0 + i, change),
            )
        conn.commit()
        assert repo.has_valid_change_pct("2026-07-24", "2026-07-24", threshold=0.5) is False

    def test_has_valid_change_pct_returns_true_when_above_threshold(self):
        db = Database(":memory:")
        repo = KlineRepo(db)
        conn = db.get_connection()
        repo._ensure_table()
        for i in range(10):
            change = 1.0 if i < 8 else 0.0
            conn.execute(
                "INSERT INTO daily_klines (code, date, close, change_pct) VALUES (?, ?, ?, ?)",
                (f"00000{i}", "2026-07-24", 10.0 + i, change),
            )
        conn.commit()
        assert repo.has_valid_change_pct("2026-07-24", "2026-07-24", threshold=0.7) is True

    def test_get_max_date_returns_none_when_empty(self):
        db = Database(":memory:")
        repo = KlineRepo(db)
        repo._ensure_table()
        assert repo.get_max_date() is None

    def test_get_max_date_returns_latest_date(self):
        db = Database(":memory:")
        repo = KlineRepo(db)
        conn = db.get_connection()
        repo._ensure_table()
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", "2026-07-21", 10.0))
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", "2026-07-24", 11.0))
        conn.commit()
        assert repo.get_max_date() == "2026-07-24"
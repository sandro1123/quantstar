"""Tests for strategy layer 3-level factor cache"""
import time
from common.database import Database
from strategy_layer.cache import FactorCache, _get_valid_columns
from strategy_layer.factors.base import FactorResult


class TestFactorCache:
    def test_get_returns_none_on_empty_cache(self):
        cache = FactorCache()
        result = cache.get("000001", "2026-07-24", "momentum")
        assert result is None
        assert cache._stats["miss"] == 1

    def test_put_and_get_from_memory(self):
        cache = FactorCache()
        result = FactorResult(code="000001", date="2026-07-24", factor_name="momentum", score=1.5)
        cache.put("000001", "2026-07-24", "momentum", result)
        cached = cache.get("000001", "2026-07-24", "momentum")
        assert cached is not None
        assert cached.score == 1.5
        assert cache._stats["memory_hit"] == 1

    def test_get_from_db_when_memory_misses(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS factor_daily (code TEXT, date TEXT, momentum REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT INTO factor_daily (code, date, momentum) VALUES (?, ?, ?)",
                     ("000001", "2026-07-24", 2.5))
        conn.commit()
        # Reset global column cache
        import strategy_layer.cache as c
        c._VALID_COLUMNS = None
        _get_valid_columns(db)

        cache = FactorCache(db=db)
        result = cache.get("000001", "2026-07-24", "momentum")
        assert result is not None
        assert result.score == 2.5
        assert cache._stats["db_hit"] == 1

    def test_cache_evicts_when_full(self):
        cache = FactorCache(max_memory=3)
        for i in range(5):
            r = FactorResult(code=f"{i:06d}", date="2026-07-24", factor_name="test", score=float(i))
            cache.put(f"{i:06d}", "2026-07-24", "test", r)
        assert len(cache._memory) == 3

    def test_cache_expires_after_ttl(self):
        cache = FactorCache(ttl_s=0)
        result = FactorResult(code="000001", date="2026-07-24", factor_name="test", score=1.0)
        cache.put("000001", "2026-07-24", "test", result)
        time.sleep(0.01)
        cached = cache.get("000001", "2026-07-24", "test")
        assert cached is None

    def test_put_ignores_non_numeric_scores(self):
        cache = FactorCache()
        result = FactorResult(code="000001", date="2026-07-24", factor_name="test", score=float("nan"))
        cache.put("000001", "2026-07-24", "test", result)
        assert cache._stats["miss"] == 0

    def test_stats(self):
        cache = FactorCache()
        assert cache.stats()["hit_rate"] == 0
        cache.get("000001", "2026-07-24", "test")
        stats = cache.stats()
        assert stats["total"] == 1
        assert stats["miss"] == 1

    def test_clear_resets_cache(self):
        cache = FactorCache()
        result = FactorResult(code="000001", date="2026-07-24", factor_name="test", score=1.0)
        cache.put("000001", "2026-07-24", "test", result)
        cache.clear()
        assert len(cache._memory) == 0
        assert cache._stats["memory_hit"] == 0

    def test_set_db(self):
        cache = FactorCache()
        db = Database(":memory:")
        cache.set_db(db)
        assert cache._db is db
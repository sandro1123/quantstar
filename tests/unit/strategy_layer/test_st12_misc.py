"""ST-12 杂项: 孤儿表达式删除 + 并行因子计算补缓存写入 + market_regime 量能假维度标注

S-19: generator.py 孤儿表达式 (计算结果未赋值) 已删除。
S-18: 并行路径子进程结果回父进程后批量 cache.put, 与串行路径一致。
S-16: market_regime 量能维度为假维度 (涨跌幅变换), docstring 标注真相。
"""
from unittest.mock import MagicMock, patch

from strategy_layer.engine import FactorEngine, FactorResult


class FakePoolExecutor:
    """假 ProcessPoolExecutor: map 直接返回预置分块结果"""

    def __init__(self, results):
        self._results = results

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def map(self, fn, tasks):
        return iter(self._results)


class TestParallelCacheWrite:
    def test_parallel_results_populate_parent_cache(self):
        """验收: workers>1 并行路径完成后, 结果写入父进程 FactorCache (原实现缓存完全失效)"""
        db = MagicMock()
        db.db_path = "/tmp/fake.db"
        engine = FactorEngine(data_layer=None, db=db)
        codes = [f"{i:06d}.SZ" for i in range(20)]
        fake_results = [
            FactorResult(code=codes[0], date="2024-01-02", factor_name="momentum_20d",
                         score=1.0, raw_value=1.0),
        ]
        with patch("strategy_layer.engine.ProcessPoolExecutor",
                   lambda max_workers, mp_context: FakePoolExecutor([fake_results])):
            with patch.object(engine, "_clean", side_effect=lambda r, d: r):
                engine.compute_all(codes, "2024-01-02", workers=2)
        cached = engine.cache.get(codes[0], "2024-01-02", "momentum_20d")
        assert cached is not None
        assert cached.score == 1.0

    def test_parallel_non_numeric_results_not_cached(self):
        """score 非数值的结果不进缓存 (cache.put 内部守卫)"""
        db = MagicMock()
        db.db_path = "/tmp/fake.db"
        engine = FactorEngine(data_layer=None, db=db)
        codes = [f"{i:06d}.SZ" for i in range(20)]
        fake_results = [
            FactorResult(code=codes[0], date="2024-01-02", factor_name="bad_factor",
                         score=None, raw_value=None),
        ]
        with patch("strategy_layer.engine.ProcessPoolExecutor",
                   lambda max_workers, mp_context: FakePoolExecutor([fake_results])):
            with patch.object(engine, "_clean", side_effect=lambda r, d: r):
                engine.compute_all(codes, "2024-01-02", workers=2)
        assert engine.cache.get(codes[0], "2024-01-02", "bad_factor") is None


class TestOrphanExpressionRemoved:
    def test_generator_source_has_no_orphan_expression(self):
        """S-19 回归: 源码中不再存在未赋值的孤儿表达式"""
        import inspect
        from strategy_layer.signals import generator
        src = inspect.getsource(generator)
        assert "(candidate.composite_score / abs(top_score)) * strength if top_score else 0" not in src


class TestMarketRegimeVolumeAnnotated:
    def test_docstring_marks_volume_as_fake_dimension(self):
        """S-16: market_regime docstring 必须标注量能是假维度 (真相源)"""
        from strategy_layer import StrategyLayer
        doc = StrategyLayer.market_regime.__doc__ or ""
        assert "假维度" in doc
        assert "成交量" in doc or "成交额" in doc

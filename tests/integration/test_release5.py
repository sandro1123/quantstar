"""Release 0.5.0 集成测试"""
import pytest
from strategy_layer import StrategyLayer
from strategy_layer.factors import ALL_FACTORS
from strategy_layer.ml import LinearScorer


def test_strategy_layer_full_init():
    sl = StrategyLayer()
    assert sl.factor_engine is not None
    assert sl.aggregator is not None
    assert sl.selector is not None
    assert sl.signal_generator is not None


def test_strategy_layer_run_daily_no_data():
    sl = StrategyLayer()
    result = sl.run_daily("2026-07-17")
    assert result.date == "2026-07-17"
    assert result.signals == []


def test_strategy_layer_get_scores_no_data():
    sl = StrategyLayer()
    scores = sl.get_scores("000001", "2026-07-17")
    assert scores.code == "000001"
    assert hasattr(scores, "factors")


def test_strategy_layer_market_regime():
    sl = StrategyLayer()
    regime = sl.market_regime("2026-07-17")
    assert regime in ("bull", "bear", "neutral", "oscillating")


def test_all_factors_count():
    # 68 基础 + 20 Alpha 表达式（扩展因子因bug暂时禁用；
    # LhbDramaScoreFactor ⚫ 弃用移出: 统筹 sm-a5a07066c2944051, drama_score 无源可采）
    # +4 ST-STYLE 盘口四族 (涨停基因/竞价强度/隔夜收益/情绪代理, 2026-09-10 @93f3045)
    assert len(ALL_FACTORS) == 2  # 开源版骨架注册 2 个示例因子 (mom20/vol20)


def test_ml_scorer_integration():
    s = LinearScorer()
    metrics = s.train([[1, 2, 3], [4, 5, 6], [7, 8, 9]], [1, 2, 3])
    assert metrics["r_squared"] > 0.9
    pred = s.predict([[2, 3, 4]])
    assert len(pred) == 1

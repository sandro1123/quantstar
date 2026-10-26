"""ST-09 拆假货: multiframe 假合成段与每日 degraded warning 已删除

原缺陷 (S-07): run_daily 无条件跑 MultiTimeframeScorer.score_batch 且不传周/月分数,
fallback 使周/月分=日分数 → 合成恒等于日分数, 每日白打一条 degraded warning。
修复: 假合成段删除; multiframe 缺真实周/月输入时显式报错 (不再拿日分充数)。
真实现排队 (需周/月频因子管道, 决策账本 S-06)。
"""
import logging
from unittest.mock import MagicMock, patch

import pytest

from strategy_layer import StrategyLayer
from strategy_layer.aggregation.multiframe import MultiTimeframeScorer


class TestMultiframeNoFakeFallback:
    def test_score_requires_real_weekly_monthly(self):
        scorer = MultiTimeframeScorer()
        with pytest.raises(ValueError, match="weekly_scores required"):
            scorer.score("000001", "2026-07-20", 1.0, None, None)

    def test_score_requires_real_monthly(self):
        scorer = MultiTimeframeScorer()
        with pytest.raises(ValueError, match="monthly_scores required"):
            scorer.score("000001", "2026-07-20", 1.0, [1.1, 1.2], None)

    def test_score_with_real_inputs_computes_weighted(self):
        scorer = MultiTimeframeScorer(weights={"daily": 0.5, "weekly": 0.3, "monthly": 0.2})
        r = scorer.score("000001", "2026-07-20", 1.0, [2.0], [3.0])
        assert r.composite_score == round(0.5 * 1.0 + 0.3 * 2.0 + 0.2 * 3.0, 4)

    def test_score_batch_missing_weekly_raises(self):
        scorer = MultiTimeframeScorer()
        fd = MagicMock(code="A", date="2026-07-20", composite_score=1.0)
        with pytest.raises(ValueError):
            scorer.score_batch([fd], {"A": 1.0})


class TestRunDailyNoFakeMultiframe:
    def test_no_degraded_warning_and_composite_untouched(self, caplog):
        """验收: 每日 degraded warning 消失; composite_score 不再被假合成覆写"""
        layer = StrategyLayer()
        layer.data_layer = MagicMock()
        layer.data_layer.get_risky_codes.return_value = set()
        fd = MagicMock()
        fd.code, fd.date = "000001.SZ", "2024-01-02"
        fd.factor_scores = {"a": 1.0}
        fd.recent_return = None
        fd.composite_score = 1.234
        fd.ml_score = 0.0
        with patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()):
            with patch.object(layer, "_get_stock_codes", return_value=["000001.SZ"]):
                with patch.object(layer.aggregator, "aggregate", return_value=[fd]):
                    with patch.object(layer.aggregator, "fill_recent_returns"):
                        with patch.object(layer, "market_regime", return_value="neutral"):
                            with patch("strategy_layer.ml.registry.ModelRegistry") as reg_cls:
                                reg_cls.return_value.list_versions.return_value = []
                                with patch.object(layer.selector, "run",
                                                  return_value=MagicMock(candidates=[])):
                                    with patch.object(layer.deep_review, "review_candidates",
                                                      return_value=[]):
                                        with patch.object(layer.signal_generator, "generate",
                                                          return_value=[]):
                                            with caplog.at_level(logging.WARNING):
                                                layer.run_daily("2024-01-02")
        assert not any("Multi-timeframe scoring degraded" in m for m in caplog.messages)
        # ml_score=0 不混合 → composite 保持聚合原值, 未被假合成覆写
        assert fd.composite_score == 1.234

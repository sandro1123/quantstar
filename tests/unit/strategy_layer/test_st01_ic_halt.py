"""ST-01 止血守护: run_daily inline IC 路径只 record_ic 观测, 不再写 factor_weights。

背景 (审计 S-01/S-02, 决策账本 S-03): forward_return 对最新日的 fallback 恒为 0,
IC 衰减据此把专家权重稀释成近均匀分布。止血 = 停写权重、保留观测;
权重重建/IC环修复延后 (三段式: 止血 → record-only → 周频+人在回路审批调权)。
"""
from unittest.mock import MagicMock, patch

from strategy_layer import StrategyLayer


def _make_fd(code="000001.SZ", date="2024-01-02"):
    fd = MagicMock()
    fd.code = code
    fd.date = date
    fd.factor_scores = {"momentum_20d": 1.0}
    fd.forward_return = 0.05
    fd.composite_score = 1.0
    fd.ml_score = 0.0
    return fd


def _run_daily_with_mocks(layer, factor_daily):
    """跑 run_daily, mock 掉取数/选股/信号, 只让 IC 反馈段走真实分支逻辑。"""
    with patch.object(layer.factor_engine, "compute_all", return_value=MagicMock()):
        with patch.object(layer, "_get_stock_codes", return_value=["000001.SZ"]):
            with patch.object(layer.aggregator, "aggregate", return_value=factor_daily):
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
                                        return layer.run_daily("2024-01-02")


class TestST01Halt:
    def _layer(self):
        layer = StrategyLayer()
        layer.data_layer = MagicMock()
        layer.data_layer.get_risky_codes.return_value = set()
        return layer

    def test_record_ic_still_called_observation_preserved(self):
        """止血保留观测: record_ic 仍被调用"""
        layer = self._layer()
        with patch("strategy_layer.feedback.persistence.WeightPersistence") as wp_cls:
            wp = wp_cls.return_value
            wp.load_weights.return_value = {"momentum_20d": 0.5}
            with patch("strategy_layer.feedback.ic_tuner.ICTuner") as tuner_cls:
                tuner_cls.return_value.compute_ic.return_value = {"momentum_20d": 0.1}
                _run_daily_with_mocks(layer, [_make_fd()])
        wp.record_ic.assert_called_once()

    def test_weight_update_halted_no_save_no_record_weights(self):
        """止血核心: 有权重时也不再 update/save/record_weights"""
        layer = self._layer()
        with patch("strategy_layer.feedback.persistence.WeightPersistence") as wp_cls:
            wp = wp_cls.return_value
            wp.load_weights.return_value = {"momentum_20d": 0.5}
            with patch("strategy_layer.feedback.ic_tuner.ICTuner") as tuner_cls:
                tuner = tuner_cls.return_value
                tuner.compute_ic.return_value = {"momentum_20d": 0.1}
                _run_daily_with_mocks(layer, [_make_fd()])
        tuner.update_weights_with_decay.assert_not_called()
        wp.save_weights.assert_not_called()
        wp.record_weights.assert_not_called()

    def test_first_run_no_longer_seeds_default_weights(self):
        """首次运行 (无持久化权重) 也不再 seed DEFAULT_WEIGHTS 写库"""
        layer = self._layer()
        with patch("strategy_layer.feedback.persistence.WeightPersistence") as wp_cls:
            wp = wp_cls.return_value
            wp.load_weights.return_value = None
            with patch("strategy_layer.feedback.ic_tuner.ICTuner") as tuner_cls:
                tuner_cls.return_value.compute_ic.return_value = {"momentum_20d": 0.1}
                _run_daily_with_mocks(layer, [_make_fd()])
        wp.save_weights.assert_not_called()

    def test_no_forward_returns_no_ic_write_at_all(self):
        """无前向收益时 record_ic 也不调用 (与原行为一致)"""
        layer = self._layer()
        fd = _make_fd()
        fd.forward_return = None
        with patch("strategy_layer.feedback.persistence.WeightPersistence") as wp_cls:
            wp = wp_cls.return_value
            wp.load_weights.return_value = {"momentum_20d": 0.5}
            with patch("strategy_layer.feedback.ic_tuner.ICTuner") as tuner_cls:
                tuner_cls.return_value.compute_ic.return_value = {}
                _run_daily_with_mocks(layer, [fd])
        wp.record_ic.assert_not_called()
        wp.save_weights.assert_not_called()

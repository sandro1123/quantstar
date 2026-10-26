"""AG-01: Agent 评估闭环 — evaluate_predictions 消费 DL-24 真实前向收益

先写失败测试: ops 原传空 dict → evaluate() 用 .get(code, 0) 把未兑现收益造假为 0,
565 条预测全按"0 收益"评估。新契约: 未兑现收益 (不在截面/None) 跳过不造假,
hit 保持 NULL 等后续交易日回填。

评估口径 (统一):
- forward_returns 为小数口径 (0.05 = +5%); DL-24 get_fwd_returns 返回百分点,
  evaluate_predictions() 换算成小数后调用 evaluate()
- hit 判定: 推荐+actual>0 / 不推荐+actual<0 → hit=1;
  观望/警惕 + |actual|<2% → hit=1 (判对无明确方向); 其余 hit=0
- 收益缺失 → 跳过 (skipped 计数), 绝不记造假评估
"""
from unittest.mock import MagicMock

import pytest

from agent_layer.core.router import AgentRouter
from agent_layer.quality.evaluator import Evaluator
from agent_layer.quality.tracker import QualityTracker
from common.database import Database


def _make_ev_with_prediction(code, verdict, date="2026-08-10", agent_name="stock_agent"):
    db = Database(":memory:")
    ev = Evaluator(database=db)
    ev.repo.write_prediction("r1", agent_name, code, date, verdict, 0.8)
    return db, ev


def _get_row(db):
    return db.get_connection().execute(
        "SELECT hit, actual_return FROM agent_predictions").fetchone()


class TestNoneReturnSkipped:
    """先写失败测试: 未兑现收益跳过不造假"""

    def test_empty_cross_section_skipped_not_fabricated(self):
        """截面为空 (收益未兑现) → 跳过, hit 保持 NULL, 不造假 0"""
        db, ev = _make_ev_with_prediction("000001", "推荐")
        result = ev.evaluate("2026-08-10", {})
        assert result["evaluated"] == 0
        assert result["skipped"] == 1
        row = _get_row(db)
        assert row["hit"] is None, "未兑现收益应保持 hit NULL, 不能造假 0 收益评估"
        assert row["actual_return"] is None

    def test_none_value_in_cross_section_skipped(self):
        """截面里显式 None 值 → 跳过不造假"""
        db, ev = _make_ev_with_prediction("000001", "推荐")
        result = ev.evaluate("2026-08-10", {"000001": None})
        assert result["evaluated"] == 0
        assert result["skipped"] == 1
        assert _get_row(db)["hit"] is None


class TestEvaluatePredictions:
    """评估核心: 读 agent_predictions + DL-24 get_fwd_returns 回填 hit/actual_return"""

    def test_consumes_fwd_returns_percentage_points(self):
        """DL-24 返回百分点 (5.0=+5%), 换算小数后评估; 推荐+正 → hit"""
        db, ev = _make_ev_with_prediction("000001", "推荐")
        data_layer = MagicMock()
        data_layer.get_fwd_returns.return_value = {"000001": 5.0}
        result = ev.evaluate_predictions("2026-08-10", data_layer=data_layer)
        assert result["hits"] == 1
        assert result["evaluated"] == 1
        row = _get_row(db)
        assert row["hit"] == 1
        assert row["actual_return"] == pytest.approx(0.05), "落库用小数口径"
        data_layer.get_fwd_returns.assert_called_once_with("2026-08-10", 5)

    def test_recommend_negative_return_miss(self):
        """推荐+负收益 → miss"""
        db, ev = _make_ev_with_prediction("000001", "推荐")
        data_layer = MagicMock()
        data_layer.get_fwd_returns.return_value = {"000001": -3.0}
        result = ev.evaluate_predictions("2026-08-10", data_layer=data_layer)
        assert result["misses"] == 1
        row = _get_row(db)
        assert row["hit"] == 0
        assert row["actual_return"] == pytest.approx(-0.03)

    def test_partial_cross_section_skips_missing_codes(self):
        """截面只覆盖部分预测 → 覆盖的评估, 缺席的跳过不造假"""
        db = Database(":memory:")
        ev = Evaluator(database=db)
        ev.repo.write_prediction("r1", "stock_agent", "000001", "2026-08-10", "推荐", 0.8)
        ev.repo.write_prediction("r2", "stock_agent", "000002", "2026-08-10", "不推荐", 0.7)
        data_layer = MagicMock()
        data_layer.get_fwd_returns.return_value = {"000001": 2.0}  # 000002 未兑现
        result = ev.evaluate_predictions("2026-08-10", data_layer=data_layer)
        assert result["evaluated"] == 1
        assert result["hits"] == 1
        assert result["skipped"] == 1
        rows = db.get_connection().execute(
            "SELECT code, hit FROM agent_predictions ORDER BY code").fetchall()
        assert rows[0]["hit"] == 1
        assert rows[1]["hit"] is None, "未兑现的 000002 保持 NULL"

    def test_no_data_layer_degrades_honestly(self):
        """无 data_layer → 诚实降级全跳过, 不造假"""
        db, ev = _make_ev_with_prediction("000001", "推荐")
        result = ev.evaluate_predictions("2026-08-10", data_layer=None)
        assert result["evaluated"] == 0
        assert result["skipped"] == 1
        assert _get_row(db)["hit"] is None

    def test_missing_read_api_degrades_honestly(self):
        """data_layer 无 get_fwd_returns 读 API (DL-24 未合并) → 诚实降级"""
        db, ev = _make_ev_with_prediction("000001", "推荐")
        data_layer = MagicMock(spec=[])  # 无 get_fwd_returns 属性
        result = ev.evaluate_predictions("2026-08-10", data_layer=data_layer)
        assert result["evaluated"] == 0
        assert result["skipped"] == 1
        assert _get_row(db)["hit"] is None

    def test_api_failure_degrades_honestly(self):
        """get_fwd_returns 抛异常 → 诚实降级, 不造假"""
        db, ev = _make_ev_with_prediction("000001", "推荐")
        data_layer = MagicMock()
        data_layer.get_fwd_returns.side_effect = RuntimeError("db down")
        result = ev.evaluate_predictions("2026-08-10", data_layer=data_layer)
        assert result["evaluated"] == 0
        assert result["skipped"] == 1
        assert _get_row(db)["hit"] is None


class TestRouterConsumesEvaluation:
    """Router/质量反馈读侧消费评估结果 (写侧已有, 接读侧)"""

    def test_router_bonus_reads_real_accuracy(self):
        """评估回填 hit 后, Router 读侧能读到真实准确率并产生 bonus"""
        db = Database(":memory:")
        ev = Evaluator(database=db)
        # agent_name 与 router 查询的 task 一致 (get_accuracy(task) 按 agent_name 过滤)
        ev.repo.write_prediction("r1", "stock_analysis", "000001", "2026-08-10", "推荐", 0.8)
        ev.repo.write_prediction("r2", "stock_analysis", "000002", "2026-08-10", "推荐", 0.8)
        ev.evaluate("2026-08-10", {"000001": 0.05, "000002": 0.03})  # 2 hit → accuracy 1.0
        tracker = QualityTracker(db)
        router = AgentRouter(quality_tracker=tracker)
        bonus = router._get_accuracy_bonus("stock_analysis")
        assert bonus == pytest.approx(0.1), "(1.0 - 0.5) * 0.2 = 0.1"

    def test_router_no_eval_data_neutral(self):
        """无评估数据 → 中性 bonus 0, 不假惩罚 (原 accuracy=0.0 → -0.1)"""
        db = Database(":memory:")
        tracker = QualityTracker(db)
        router = AgentRouter(quality_tracker=tracker)
        assert router._get_accuracy_bonus("stock_analysis") == 0

    def test_agent_layer_wires_tracker_to_router(self):
        """AgentLayer 构造时把 quality_tracker 接入 Router"""
        from agent_layer import AgentLayer
        layer = AgentLayer(Database(":memory:"))
        assert layer._router._quality_tracker is layer._quality_tracker
        layer.shutdown()

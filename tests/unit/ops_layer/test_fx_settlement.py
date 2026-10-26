# KLINE-FX 结构修卡 (统筹 2026-09-07) 单测
# 病: 17:40 kline 兜底补满后无 factor 重跑排程 → 部分面日因子停 9.4%
# 修: fx_settlement 18:05 幂等哨 — 因子行>=K线行短路, 缺口则补跑两 handler
import os
from unittest.mock import MagicMock


def _orch_with_faces(kline_rows, factor_rows, factor_after=None,
                     fx_ok=True, fwd_meta=None):
    """构造 orchestrator 桩: db 面计数按序返回, 两个子 handler 全打桩。"""
    from ops_layer.orchestrator import PipelineOrchestrator
    orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
    conn = MagicMock()
    counts = [kline_rows, factor_rows]
    if factor_after is not None:
        counts.append(factor_after)
    conn.execute.return_value.fetchone.side_effect = [(c,) for c in counts]
    db = MagicMock()
    db.get_connection.return_value = conn
    orch.db = db
    fx = MagicMock()
    fx.success = fx_ok
    fx.errors = []
    fwd = MagicMock()
    fwd.errors = []
    fwd.metadata = fwd_meta or {"filled_total": 0}
    orch._run_factor_update = MagicMock(return_value=fx)
    orch._run_fwd_return_backfill = MagicMock(return_value=fwd)
    return orch, fx, fwd


class TestGuardShortCircuit:
    def test_no_klines_skips(self):
        orch, _, _ = _orch_with_faces(0, 0)
        r = orch._run_fx_settlement({})
        assert r.metadata["skipped"] is True
        assert r.metadata["reason"] == "no_klines"
        orch._run_factor_update.assert_not_called()

    def test_full_face_skips(self):
        orch, _, _ = _orch_with_faces(5549, 5549)
        r = orch._run_fx_settlement({})
        assert r.metadata["skipped"] is True
        assert r.metadata["reason"] == "factor_face_complete"
        orch._run_factor_update.assert_not_called()

    def test_factor_exceeds_kline_also_skips(self):
        # >= 判定边缘: 因子行超 K线行同样短路
        orch, _, _ = _orch_with_faces(5540, 5541)
        r = orch._run_fx_settlement({})
        assert r.metadata["skipped"] is True
        assert r.metadata["reason"] == "factor_face_complete"


class TestGapRepair:
    def test_partial_day_triggers_both_handlers(self):
        # 09-07 实证形态: kline 5549 (17:40 后), factor 523 (毒面)
        orch, _, _ = _orch_with_faces(5549, 523, factor_after=5549)
        r = orch._run_fx_settlement({})
        orch._run_factor_update.assert_called_once()
        orch._run_fwd_return_backfill.assert_called_once()
        assert r.metadata["repaired"] is True
        assert r.metadata["factor_before"] == 523
        assert r.metadata["factor_after"] == 5549
        assert r.metadata["partial"] is False
        assert r.metadata["partial_gap"] == 0

    def test_repair_residue_is_partial(self):
        # 补跑后仍不满 → partial 留痕 (铁律 #12, DL-43 先例)
        orch, _, _ = _orch_with_faces(5549, 523, factor_after=3000)
        r = orch._run_fx_settlement({})
        assert r.metadata["partial"] is True
        assert r.metadata["partial_gap"] == 2549
        assert r.final_status == "partial"

    def test_fwd_meta_passthrough(self):
        orch, _, _ = _orch_with_faces(100, 10, factor_after=100,
                                      fwd_meta={"filled_total": 522})
        r = orch._run_fx_settlement({})
        assert r.metadata["fwd_filled_total"] == 522

    def test_fx_failure_propagates(self):
        # factor_update 失败 → fx_settlement success=False (不假绿)
        orch, _, _ = _orch_with_faces(100, 10, factor_after=10, fx_ok=False)
        r = orch._run_fx_settlement({})
        assert r.success is False
        assert r.final_status == "failed"


class TestRegistry:
    def test_handler_method_exists(self):
        from ops_layer.orchestrator import PipelineOrchestrator
        assert hasattr(PipelineOrchestrator, "_run_fx_settlement")

    def test_ops_yaml_block(self):
        import yaml
        # tests/unit/ops_layer/ → 上溯三级到仓库根 (config/defaults/ops.yaml)
        root = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
        ops_path = os.path.join(root, "config", "defaults", "ops.yaml")
        with open(ops_path) as f:
            d = yaml.safe_load(f)
        fx = d["ops"]["pipelines"]["fx_settlement"]
        assert fx["schedule"] == "18:05"
        assert fx["days"] == "1-5"
        assert fx["depends_on"] == []
        assert fx["timeout_s"] == 900
        assert fx["max_retries"] == 1

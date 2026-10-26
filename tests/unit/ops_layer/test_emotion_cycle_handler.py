"""R1 修复卡 (统筹 sm-d32393cc82af4207 裁定立卡修):

_run_emotion_cycle handler success 未接 persisted 位 — 落库失败仍
success = 「降级不告警不落 runs」纪律③反例实锤。

病版 (dc9c998e 原版):
    success=bool(out) and not out.get("error")
    — out {"date","stage","hit","persisted":False} 无 error 键 →
      bool(out) True → success True。落库失败被 logger.warning 吞掉,
      pipeline_runs 无感 = 静默病。

红-绿: 病版下 test_persisted_false_fails 必红; 修版 (success 接
persisted 位) 三测全绿。撤回修版任一分支断言必红。

判例应用:
- from-import 绑定名须 patch 模块属性非 string 形 (695d804b) —
  handler 内 `from strategy_layer.regime.persist import
  run_emotion_cycle_daily` 是函数内延迟导入, patch 目标 =
  strategy_layer.regime.persist.run_emotion_cycle_daily 本尊。
"""
from unittest.mock import MagicMock, patch


def _orch():
    """零依赖构造 PipelineOrchestrator (不触 __init__ 的 app 组装)。"""
    from ops_layer import orchestrator as orch
    app = MagicMock()
    orch_obj = orch.PipelineOrchestrator.__new__(orch.PipelineOrchestrator)
    orch_obj._get_app = lambda: app
    return orch_obj, app


class TestEmotionCycleHandlerPersistedFace:
    """纪律③: 降级必告警必留痕 — 落库失败不得伪装成功。"""

    def test_persisted_false_fails(self):
        """run_emotion_cycle_daily 返回 persisted=False (落库失败,
        logger.warning 有痕但 runs 无感) → handler 必 success=False
        带 error, 让 pipeline_runs 记失败。"""
        orch_obj, app = _orch()
        out = {"date": "2026-09-14", "stage": "recovery", "hit": "recovery_plateau",
               "persisted": False}
        with patch("strategy_layer.regime.persist.run_emotion_cycle_daily",
                   return_value=out):
            result = orch_obj._run_emotion_cycle({"date": "2026-09-14"})
        assert result.success is False, (
            "persisted=False 仍 success=True — 落库失败静默病 (纪律③反例), "
            "pipeline_runs 会把没写进表的首火记成绿"
        )
        assert result.errors, "persisted=False 必须带 error 文案 (不留痕即静默)"

    def test_persisted_true_success(self):
        """正常腿: persisted=True → success=True, metadata 带 stage。"""
        orch_obj, app = _orch()
        out = {"date": "2026-09-14", "stage": "recovery", "hit": "recovery_plateau",
               "persisted": True}
        with patch("strategy_layer.regime.persist.run_emotion_cycle_daily",
                   return_value=out):
            result = orch_obj._run_emotion_cycle({"date": "2026-09-14"})
        assert result.success is True
        assert (result.metadata or {}).get("stage") == "recovery"

    def test_exception_fails_with_error(self):
        """接腿抛异常 → success=False + errors 带 emotion_cycle 前缀
        (既有分支回归锁 — 修 persisted 位不得破坏此腿)。"""
        orch_obj, app = _orch()
        with patch("strategy_layer.regime.persist.run_emotion_cycle_daily",
                   side_effect=RuntimeError("db locked")):
            result = orch_obj._run_emotion_cycle({"date": "2026-09-14"})
        assert result.success is False
        assert result.errors and "emotion_cycle" in str(result.errors[0])

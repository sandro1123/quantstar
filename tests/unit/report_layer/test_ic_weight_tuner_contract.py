"""ICWeightSection ↔ ICTuner 契约 — 无参 get_factor_ic() 不得静默降级。

病 (F席 E6 侦察 09-16): src/report_layer/sections/ic_weight.py:38-43
调 `tuner.get_factor_ic()`(无参), 而 ICTuner.get_factor_ic 签名**必填
factor_name** → TypeError → 被 except 吞成 note_degraded(降级) → 回退
只读 mean_ic。调用方意图明写为「取全因子 IC」(返回 {name: ic} 供渲染),
被调方无此形态 = 契约不一致 (「被 except 吞掉的错」同族)。

注: 该分支当前**不可达**(StrategyLayer 从不设 ic_feedback_tuner/_ic_tuner
属性, 活体实测 getattr=MISSING), 且 ICWeightSection 生产零注册 (已移出晨报)。
本卡修契约、拆潜伏雷 (零行为变化: 生产仍走 DB 备用路径)。
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "src"))

from report_layer.reports.base import ReportContext  # noqa: E402
from report_layer.sections.ic_weight import ICWeightSection  # noqa: E402
from strategy_layer.feedback.ic_tuner import ICTuner  # noqa: E402


def _tuner_with_history():
    t = ICTuner()
    t._ic_history = {
        "momentum_20d": [0.05, 0.06, 0.04, 0.07, 0.05],
        "roe_quality": [0.03, 0.02, 0.04, 0.03, 0.02],
    }
    return t


def test_no_arg_returns_all_factors_dict():
    """get_factor_ic() 无参 → 返回 {factor: mean_ic} (调用方意图形态)。"""
    out = _tuner_with_history().get_factor_ic()
    assert isinstance(out, dict), f"应为 dict, 实为 {type(out)}"
    assert set(out) == {"momentum_20d", "roe_quality"}
    assert out["momentum_20d"] == pytest.approx(0.054)


def test_named_arg_still_returns_factoric():
    """向后兼容: 传因子名仍返回 FactorIC (既有契约不破)。"""
    fc = _tuner_with_history().get_factor_ic("momentum_20d")
    assert fc is not None and fc.factor_name == "momentum_20d"
    assert _tuner_with_history().get_factor_ic("nonexistent") is None


def test_section_tuner_branch_supplies_data():
    """段走 tuner 路径时须供数渲染, 不得因无参调用炸而静默降级。"""
    from unittest.mock import MagicMock
    sl = MagicMock()
    sl.ic_feedback_tuner = _tuner_with_history()
    result = ICWeightSection().build(
        ReportContext(date="2026-09-16", strategy_layer=sl))
    assert result is not None
    assert "动量(20日)" in result.body, \
        f"tuner 路径未供数 (无参调用被吞): {result.body!r}"
    assert "IC数据暂不可用" not in result.body

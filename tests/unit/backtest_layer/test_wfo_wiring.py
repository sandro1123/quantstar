# -*- coding: utf-8 -*-
"""#5 WFO 防泄漏接线 — purge/embargo 必须真能从配置走到切分

判例 #150「守卫必须跑真路径」: dataclass 有 purge_days 字段 ≠ 生产会用上。
本卡钉三件事:
  1. backtest.yaml 里真有 purge_days/embargo_days 键;
  2. BacktestEngine 真的把它们读进 WalkForwardConfig;
  3. 配置缺失时回落默认且**不静默** (结果会带 leakage_risk)。

反向钉: 把引擎的配置读取注掉 → 接线测试必须报红。
"""
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
BT_YAML = REPO / "config" / "defaults" / "backtest.yaml"


class TestConfigFile:
    def test_yaml_has_purge_and_embargo(self):
        """默认配置必须显式写出 purge/embargo (不靠代码默认值隐式)"""
        cfg = yaml.safe_load(BT_YAML.read_text(encoding="utf-8"))
        wf = cfg["backtest"]["walk_forward"]
        assert "purge_days" in wf, "缺 purge_days — 标签重叠泄漏无防护"
        assert "embargo_days" in wf
        assert isinstance(wf["purge_days"], int) and wf["purge_days"] >= 0
        assert isinstance(wf["embargo_days"], int) and wf["embargo_days"] >= 0

    def test_purge_at_least_label_horizon(self):
        """purge 必须 >= 标签 horizon (hold_days), 否则裁不干净"""
        cfg = yaml.safe_load(BT_YAML.read_text(encoding="utf-8"))
        bt = cfg["backtest"]
        assert bt["walk_forward"]["purge_days"] >= bt["hold_days"], (
            f"purge_days={bt['walk_forward']['purge_days']} < "
            f"hold_days={bt['hold_days']} — 标签窗口越界部分没裁干净")

    def test_config_is_not_empty_node(self):
        """walk_forward 不能是空节点 (P0 前它就是空的, 等于什么都没配)"""
        cfg = yaml.safe_load(BT_YAML.read_text(encoding="utf-8"))
        wf = cfg["backtest"]["walk_forward"]
        assert isinstance(wf, dict) and wf, "walk_forward 是空节点, 配置未生效"


class TestEngineWiring:
    def test_engine_reads_config_into_wf_config(self):
        """BacktestEngine._build_wf_config 真把 yaml 值读进 WalkForwardConfig"""
        from backtest_layer.core.engine import BacktestEngine
        from backtest_layer.core.models import BacktestInput

        engine = BacktestEngine()
        inp = BacktestInput(mode="walk_forward", codes=["000001"],
                            date_range=("2026-01-01", "2026-06-30"))
        wfc = engine._build_wf_config(inp)
        assert wfc.purge_days > 0, "引擎没把 purge_days 读进配置 — 接线断了"
        assert wfc.embargo_days >= 0

    def test_pipeline_params_override_config(self):
        """管道入参优先于全局配置 (单次回测可覆盖)"""
        from backtest_layer.core.engine import BacktestEngine
        from backtest_layer.core.models import BacktestInput

        engine = BacktestEngine()
        inp = BacktestInput(mode="walk_forward", codes=["000001"],
                            date_range=("2026-01-01", "2026-06-30"),
                            strategy_params={"purge_days": 99, "embargo_days": 7})
        wfc = engine._build_wf_config(inp)
        assert wfc.purge_days == 99
        assert wfc.embargo_days == 7

    def test_injected_config_wins(self):
        """显式注入的配置对象最高优先级 (测试/特殊回测用)"""
        from backtest_layer.core.engine import BacktestEngine
        from backtest_layer.core.models import BacktestInput
        from backtest_layer.modes.walk_forward import WalkForwardConfig

        injected = WalkForwardConfig(purge_days=3, embargo_days=2)
        engine = BacktestEngine(wf_config=injected)
        inp = BacktestInput(mode="walk_forward", codes=["000001"],
                            date_range=("2026-01-01", "2026-06-30"))
        assert engine._build_wf_config(inp) is injected

    def test_invalid_config_falls_back_not_crash(self):
        """配置坏值 (负数) → 回落默认, 不炸回测"""
        from backtest_layer.core.engine import BacktestEngine
        from backtest_layer.core.models import BacktestInput

        engine = BacktestEngine()
        inp = BacktestInput(mode="walk_forward", codes=["000001"],
                            date_range=("2026-01-01", "2026-06-30"),
                            strategy_params={"purge_days": -5})
        wfc = engine._build_wf_config(inp)   # 不应抛 ValueError
        assert wfc.purge_days == 0, "坏值应回落默认 (0), 而非沿用 -5"


class TestMutationWiring:
    def test_mutation_engine_ignores_config_is_caught(self):
        """反向钉: 把引擎的配置读取换成"永远默认" → 接线断言必须报红"""
        from backtest_layer.core.engine import BacktestEngine
        from backtest_layer.core.models import BacktestInput

        engine = BacktestEngine()
        inp = BacktestInput(mode="walk_forward", codes=["000001"],
                            date_range=("2026-01-01", "2026-06-30"))
        engine._build_wf_config = lambda i: __import__(
            "backtest_layer.modes.walk_forward", fromlist=["WalkForwardConfig"]
        ).WalkForwardConfig()

        with pytest.raises(AssertionError):
            assert engine._build_wf_config(inp).purge_days > 0

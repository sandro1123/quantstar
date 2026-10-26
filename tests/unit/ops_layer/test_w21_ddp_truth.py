# -*- coding: utf-8 -*-
"""W2-1 (2026-09-19): fuyao_financial 移入 best_effort — 季报稀疏不得拖垮 DDP。

病根 (生产普查 14 次 failed DDP run 中 6 次此因):
  fuyao_financial 每天 partial (indicators 缺失/部分代码错) → 父 DDP failed。
  财务 = 季报 (stock_financials max=2026-06-30), 当日无新公告属 expected_no_update
  非源故障。移入 best_effort → 降级为父管道 partial + 具名留痕, 不阻塞核心路径
  (照 lhb_daily/index_daily 先例)。

⚠️ kline 停牌幻影修法 (原 A 段, 60 天无 K线判「预期空」) 已按审计校准红线
  「不可用未采到推停牌」撤出 (09-19), 存证 /tmp/kline_w21_rejected_20260919.py,
  周一按丙案返工: unknown_gap 显形表 + 双指标覆盖率, 见 test_w2_audit_gates.py
  族二两条 xfail 锚。
"""


def test_fuyao_financial_is_best_effort():
    """fuyao_financial 须在 best_effort 集合 (partial 降级不拖垮 DDP)。"""
    import inspect
    from ops_layer.orchestrator import PipelineOrchestrator
    src = inspect.getsource(PipelineOrchestrator._run_data_pipeline)
    assert '"fuyao_financial"' in src or "'fuyao_financial'" in src
    # 出现在 best_effort 赋值块 (多行元组)
    block = src.split("best_effort = name in")[1].split(")")[0]
    assert "fuyao_financial" in block, "fuyao_financial 不在 best_effort 集合"


def test_fuyao_degraded_leaves_named_trace():
    """fuyao 降级 = 父管道 partial + best_effort_degraded 具名 (不得绿灯吞)。"""
    import inspect
    from ops_layer.orchestrator import PipelineOrchestrator
    src = inspect.getsource(PipelineOrchestrator._run_data_pipeline)
    assert "best_effort_degraded.append" in src
    assert '"partial": True' in src or "'partial': True" in src

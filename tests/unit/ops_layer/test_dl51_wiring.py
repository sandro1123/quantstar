"""DL-51 红绿测试: kline/adjust_factor 顶层管道接线 + 第三方对账 (孤立管线检出)。

红绿说明:
  - 移除 ops.yaml 的 kline/adjust_factor 定义后, _pipeline_configs 查不到 →
    run_pipeline 返回 "pipeline not defined" → 测试 1/2 失败。
  - 移除 orchestrator dispatch 表的 _run_kline/_run_adjust_factor 后,
    _execute_pipeline 找不到 handler → 测试 3 失败。
  - 移除 check_orphan_pipelines 后, 测试 4 ImportError。
"""
import pytest

from ops_layer.schedule_check import check_orphan_pipelines


# ---- 1. ops.yaml 定义了 kline/adjust_factor 顶层管道 ----
def test_ops_yaml_defines_kline_and_adjust_factor():
    import os
    import yaml
    # 直接读 ops.yaml (不经 config_layer, 避免测试环境 CONFIG_ENV 依赖)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    yaml_path = os.path.join(root, "config", "defaults", "ops.yaml")
    with open(yaml_path) as f:
        cfg = yaml.safe_load(f)
    pipelines = cfg.get("ops", {}).get("pipelines", {})
    assert "kline" in pipelines, "ops.yaml 缺少 kline 顶层管道定义"
    assert "adjust_factor" in pipelines, "ops.yaml 缺少 adjust_factor 顶层管道定义"
    # 调度时间符合提案 (17:40 备源补跑 / 18:30 复权因子)
    assert pipelines["kline"].get("schedule") == "17:40"
    assert pipelines["adjust_factor"].get("schedule") == "18:30"


# ---- 2. orchestrator dispatch 表有 handler ----
def test_dispatch_has_handlers():
    import inspect
    from ops_layer.orchestrator import PipelineOrchestrator
    src = inspect.getsource(PipelineOrchestrator._execute_pipeline)
    assert '"kline": self._run_kline' in src, "dispatch 表缺少 kline handler"
    assert '"adjust_factor": self._run_adjust_factor' in src, "dispatch 表缺少 adjust_factor handler"
    assert hasattr(PipelineOrchestrator, "_run_kline")
    assert hasattr(PipelineOrchestrator, "_run_adjust_factor")


# ---- 3. 第三方对账: 孤立管线检出 ----
def test_orphan_detection():
    registered = ["kline", "adjust_factor", "financial", "auction_collect", "valuation"]
    top_level = {"kline": {}, "adjust_factor": {}, "daily_data_pipeline": {}}
    sub = ["valuation", "kline"]  # daily 子管线
    handler_hosted = ["auction_collect"]  # 由 _run_auction_parse 调用
    res = check_orphan_pipelines(registered, top_level, sub, handler_hosted=handler_hosted)
    # financial 无宿主 → 孤立; 其余都有宿主
    assert res["orphans"] == ["financial"]
    assert "kline" in res["wired"]
    assert "auction_collect" in res["wired"]  # handler-hosted 不算孤立


# ---- 4. 对账: 全部有宿主 → 无孤立 ----
def test_no_orphans_when_all_wired():
    registered = ["kline", "valuation"]
    top_level = {"kline": {}}
    sub = ["valuation"]
    res = check_orphan_pipelines(registered, top_level, sub)
    assert res["orphans"] == []
    assert len(res["wired"]) == 2


# ---- 5. 对账: 空注册表 → 无孤立 ----
def test_empty_registry():
    res = check_orphan_pipelines([], {"kline": {}}, ["valuation"])
    assert res["orphans"] == []
    assert res["wired"] == []

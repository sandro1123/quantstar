# -*- coding: utf-8 -*-
"""TDD 测试钉：风险收益比入池阈值从 1.5 调整为 1.45 (REC 模块2 / 2026-10-24 用户批准)。

背景与实测依据：
  数据实测表明候选池被拦截的标的密集聚在 [1.478, 1.498]（如 600886、000550、600305 等），
  因 1.5 门槛过严导致优质候选池入池发生断崖。用户裁定批准调整为 1.45。
"""
import os
import pytest
from unittest.mock import patch
from common.database import Database


@pytest.fixture
def test_db():
    db = Database(":memory:")
    db.init_db()
    return db


def _make_rec(code="600001", rr_target=1.48):
    """构造指定 rr_ratio 的买入推荐。
    entry_mid = 10.0, stop_loss = 9.0 (risk = 1.0)
    take_profit = entry_mid + rr_target * risk = 10.0 + rr_target
    """
    entry_low = 9.9
    entry_high = 10.1
    stop_loss = 9.0
    take_profit = 10.0 + rr_target
    return dict(
        code=code,
        name=f"测试{code}",
        action="buy",
        confidence=0.85,
        reason="测试理由",
        initial_score=90,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


def test_production_execution_yaml_has_min_rr_145():
    """钉1 (配置真身): execution.yaml 必须显式配置 min_rr_ratio: 1.45。"""
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    cfg_path = os.path.join(root, "config", "defaults", "execution.yaml")
    assert os.path.exists(cfg_path), f"找不到 {cfg_path}"
    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    exec_cfg = data.get("execution", {})
    assert "min_rr_ratio" in exec_cfg, "execution.yaml 必须显式声明 min_rr_ratio"
    assert exec_cfg["min_rr_ratio"] == 1.45, f"期望 min_rr_ratio=1.45, 实得: {exec_cfg.get('min_rr_ratio')}"


def test_config_layer_loads_min_rr_145(monkeypatch):
    """钉2 (配置层读取): Config.load() 后 get('execution.min_rr_ratio') 为 1.45。"""
    from config_layer import Config, reset_config
    reset_config()
    monkeypatch.setenv("FUYAO_API_KEY", "dummy_k")
    monkeypatch.setenv("FEISHU_APP_ID", "dummy_id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "dummy_sec")
    try:
        cfg = Config(profile="prod")
        cfg.load()
        val = cfg.get("execution.min_rr_ratio")
        assert val == 1.45, f"Config.get('execution.min_rr_ratio') 应为 1.45, 实得 {val}"
    finally:
        reset_config()


def test_plan_manager_reads_145_and_allows_148(test_db):
    """钉3 (入池放行): rr=1.48 (在 [1.45, 1.5) 区间) 的优质标的在 1.45 阈值下成功入池。"""
    from config_layer import Config, reset_config, get_config
    from execution_layer.core.plan_manager import PlanManager
    from execution_layer.core.stock_pool import StockPool
    from execution_layer.core.action_record import ActionRecord

    reset_config()
    cfg = get_config()
    cfg.load()

    pool = StockPool(test_db)
    pm = PlanManager(test_db, pool, ActionRecord(test_db))

    assert pm._min_rr_ratio() == 1.45, f"pm._min_rr_ratio() 必须为 1.45, 实得 {pm._min_rr_ratio()}"

    # 1.48 应该成功入池
    rec148 = _make_rec(code="600886", rr_target=1.48)
    pm.receive_plan({"recommendations": [rec148]})
    row = pool.get("600886")
    assert row is not None, "rr=1.48 的标的在 min_rr_ratio=1.45 下必须成功入池"
    assert abs(row["rr_ratio"] - 1.48) < 1e-4

    # 1.40 应该被守卫拒绝
    rec140 = _make_rec(code="000002", rr_target=1.40)
    pm.receive_plan({"recommendations": [rec140]})
    assert pool.get("000002") is None, "rr=1.40 (< 1.45) 的标的必须被守卫拒绝入池"

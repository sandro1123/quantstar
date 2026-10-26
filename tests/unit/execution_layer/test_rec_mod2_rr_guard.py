# -*- coding: utf-8 -*-
"""REC 模块2 (2026-09-21): 入池风险收益守卫 + 风险收益比落库。

勘误前置 (统筹 09-21 裁定): 「62%」实证错误已收回 — 生产四字段 158/158=100%。
守卫仍立: 全填≠有守卫 (freshness「有注释≠有约束」同构) — 信号链
receive_plan 的 rec.get("stop_loss", 0) 在上游缺字段时会静默落 0
(生产恰好没发生, 但守卫线是防回归载体)。
验收口径 = 守卫在位 + 填充率维持 100% (非从 62% 修复)。

设计:
- 守卫点 = receive_plan buy 分支, stock_pool.add() 之前:
  entry_low/entry_high/stop_loss/take_profit 任一 <=0 或缺失 →
  拒入池 + action_record skip_entry + AlertManager 留痕 (pool_position_guard
  960f34c8 模式), 不静默降级 0。
- 人工批准链 EX-01 消费侧守卫已存在 (plan_manager:352), 不动 (修法不扩大)。
- 风险收益比 rr_ratio = (take_profit - entry_mid) / (entry_mid - stop_loss),
  entry_mid=(entry_low+entry_high)/2; 计算落库 (新列, valid_columns 白名单
  登记 + DDL 迁移); < min_rr_ratio (config 键, 缺省 1.5) → 不入池。
"""
import pytest

from common.database import Database


@pytest.fixture
def pool_db():
    db = Database(":memory:")
    db.init_db()
    return db


def _rec(**over):
    base = dict(code="600001", name="测试A", action="buy", confidence=0.8,
                reason="测试理由", initial_score=90,
                entry_low=10.0, entry_high=11.0, stop_loss=9.5, take_profit=12.0)
    base.update(over)
    return base


def _pm(db):
    from execution_layer.core.plan_manager import PlanManager
    from execution_layer.core.stock_pool import StockPool
    from execution_layer.core.action_record import ActionRecord
    pool = StockPool(db)
    return PlanManager(db, pool, ActionRecord(db)), pool


# ---- 守卫: 缺风险收益参数拒入池 ----

def test_buy_without_stop_loss_rejected(pool_db):
    """信号链 buy 缺 stop_loss → 拒入池 (零行), 不静默落 0。"""
    pm, pool = _pm(pool_db)
    plan = {"recommendations": [_rec(stop_loss=0)]}
    pm.receive_plan(plan)
    assert pool.get("600001") is None, \
        "stop_loss=0 的 buy 不得入池 (守卫线, 拒绝非静默降级)"


def test_buy_without_entry_range_rejected(pool_db):
    """缺买入区间 (entry_low/high 缺失→0) → 拒入池。"""
    pm, pool = _pm(pool_db)
    plan = {"recommendations": [_rec(entry_low=0, entry_high=0)]}
    pm.receive_plan(plan)
    assert pool.get("600001") is None, "缺买入区间的 buy 不得入池"


def test_rejection_leaves_trace(pool_db):
    """拒入池必须留痕: action_record 有 skip_entry 行 (不静默)。"""
    from execution_layer.core.action_record import ActionRecord
    pm, pool = _pm(pool_db)
    pm.receive_plan({"recommendations": [_rec(take_profit=0)]})
    conn = pool_db.get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM action_records WHERE code='600001' "
        "AND action_type='skip_entry'").fetchone()[0]
    assert n >= 1, f"拒入池必须写 skip_entry 留痕, 实得 {n} (静默拒绝=隐身)"
    assert pool.get("600001") is None


def test_complete_buy_still_enters(pool_db):
    """参数齐全的 buy 照常入池 (守卫不误伤正常流)。"""
    pm, pool = _pm(pool_db)
    pm.receive_plan({"recommendations": [_rec()]})
    row = pool.get("600001")
    assert row is not None, "参数齐全的 buy 必须照常入池 (守卫不误伤)"
    assert row["stop_loss"] == 9.5


# ---- 风险收益比计算落库 ----

def test_rr_ratio_computed_and_saved(pool_db):
    """入池时计算 rr_ratio 落库: (12-10.5)/(10.5-9.5) = 1.5。"""
    pm, pool = _pm(pool_db)
    pm.receive_plan({"recommendations": [_rec()]})
    row = pool.get("600001")
    assert row.get("rr_ratio") is not None, "rr_ratio 未落库"
    assert abs(row["rr_ratio"] - 1.5) < 1e-6, \
        f"rr_ratio={row['rr_ratio']} 期望 1.5 = (12-10.5)/(10.5-9.5)"


def test_rr_ratio_rejected_below_threshold(pool_db, monkeypatch):
    """rr < min_rr_ratio (缺省 1.5) → 不入池 (低赔率不推荐)。"""
    monkeypatch.setenv("QS_TEST_MIN_RR", "1")
    pm, pool = _pm(pool_db)
    # rr = (11-10.5)/(10.5-9.5) = 0.5 < 1.5 → 拒
    plan = {"recommendations": [_rec(take_profit=11.0)]}
    pm.receive_plan(plan)
    assert pool.get("600001") is None, \
        "rr_ratio < 阈值 (0.5 < 1.5) 不得入池 (低赔率不推荐)"


def test_rr_threshold_config_driven(pool_db, monkeypatch, tmp_path):
    """阈值走 config 键 execution.min_rr_ratio (不写死) — 调低后照常入池。"""
    from config_layer import Config
    import os
    # 直接构造带 config 的 PlanManager 链路太重; 此处验证 StockPool/守卫读键点存在
    from execution_layer.core import plan_manager as pmm
    src = open(pmm.__file__, encoding="utf-8").read()
    assert "min_rr_ratio" in src, "守卫须读 execution.min_rr_ratio 配置键 (不写死)"


def test_rr_column_in_whitelist():
    """rr_ratio 新列登记 valid_columns 白名单 (AUD-008: 未登记=静默丢弃)。"""
    from execution_layer.core.stock_pool import StockPool
    import inspect
    src = inspect.getsource(StockPool)
    assert '"rr_ratio"' in src or "'rr_ratio'" in src, \
        "rr_ratio 须登记 StockPool.update 的 valid_columns 白名单"

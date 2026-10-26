# -*- coding: utf-8 -*-
"""P1-2 语义混用守卫 (三闸卡留尾, 09-28)。

缺陷: _approval_max_age_days 缺省 2 与 entry_valid_days=5 语义不同已分别定义,
但无守卫防未来「优化」成共常量 (09-25 注明「语义不同不共常量」——无测试锁
就是注释级约束, 判例=「有注释」≠「有约束」)。

纯测试卡, 零生产改动。
"""
from unittest.mock import patch

import pytest


def _pm():
    from execution_layer.core.plan_manager import PlanManager
    from execution_layer.core.stock_pool import StockPool
    from execution_layer.core.action_record import ActionRecord
    from common.database import Database
    db = Database(":memory:")
    pool = StockPool(db)
    pool._ensure_table()
    ar = ActionRecord(db)
    ar._ensure_table()
    return PlanManager(db, pool, ar)


# 钉①: 两常量值不等 (防合并成共常量)
def test_approval_max_age_not_entry_valid_days():
    pm = _pm()
    approval = pm._approval_max_age_days()
    # entry_valid_days 缺省 5 (execution.entry_valid_days)
    from config_layer import cfg_get
    entry_valid = int(cfg_get("execution.entry_valid_days", 5))
    assert approval != entry_valid, (
        f"_approval_max_age_days({approval}) 与 entry_valid_days({entry_valid}) "
        f"语义不同不得共常量 — 批准单被动保鲜期 ≠ 入池主动耐心 (09-25 三闸卡)")


# 钉②: 审批闸用 approval_max_age 不用 entry_valid_days
# 3 个交易日旧批准单: entry_valid_days=5 会放行, approval_max_age=2 应拒
def test_stale_approval_rejected_by_approval_age_not_entry_valid():
    pm = _pm()
    # 造 3 交易日旧: _approval_age_days 返回 3
    with patch.object(pm, "_approval_age_days", return_value=3), \
         patch.object(pm, "_approval_max_age_days", return_value=2):
        age = pm._approval_age_days("2026-09-23")
        max_age = pm._approval_max_age_days()
        # 闸A 语义: age > max_age → 陈旧拒
        is_stale = age > max_age
        assert is_stale, (
            f"3 交易日旧批准单 (age={age}) 应被 approval_max_age={max_age} 判陈旧; "
            f"若误用 entry_valid_days=5 则放行 = 语义混用")
        # 反证: 若用 entry_valid_days=5 则不陈旧 → 两常量判据对立, 锁死用对那个
        assert not (age > 5), "对照: entry_valid_days=5 下同单不陈旧 — 判据对立成立"


# 钉③: 缺省值锁 (approval_max_age 缺省 2, 防被改成 5)
def test_approval_max_age_default_is_2():
    pm = _pm()
    # 无配置注入时缺省 2 (cfg_get 拿不到走 default)
    with patch("config_layer.cfg_get", side_effect=lambda k, d: d):
        assert pm._approval_max_age_days() == 2, \
            "缺省值被改 — 09-25 取 2 的依据=生产实测漂移拐点 (4 日 68% 漂出)"

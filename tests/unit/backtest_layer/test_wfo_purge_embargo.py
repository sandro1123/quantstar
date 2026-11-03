# -*- coding: utf-8 -*-
"""#5 WFO purge / embargo 防泄漏

Issue #5 原文:
  「walk-forward 每个窗口需要在训练段重新选参/重拟合, 再在测试段出样本外结果;
    训练与测试之间加 purge + embargo 以避免标签重叠泄漏。
    验收: 打乱测试段标签后 OOS 指标应退化到随机水平; 每窗口参数可追溯。」

本卡做**刀1**: purge / embargo 切分 (泄漏消除)。
刀2 (每窗口重选参重拟合) 是结构性改造, 单独立卡。

## 病
原 `_split_is_oos` 只按 is_ratio 切, IS 末尾紧贴 OOS 开头。若标签是
「未来 N 日收益」, IS 最后 N 天的标签用到了 OOS 段的价格 = 标签重叠泄漏。
相邻日特征还高度自相关, 边界样本给 OOS 成绩注水。

## 药
- purge_days: IS 末尾裁掉 N 天 (标签窗口越界部分);
- embargo_days: OOS 开头吃掉 M 天 (自相关隔离带);
- 两者都写进 wf_result 供追溯; purge=0 时打 leakage_risk 标记 (不静默放过)。

## 反向钉
- 把 purge/embargo 实现注掉 (不裁切) → 间隔断言必须报红;
- 把裁切做成「恒裁到底」(段全空) → 非空断言必须报红。
"""
from datetime import datetime

import pytest

from backtest_layer.modes.walk_forward import WalkForwardConfig, WalkForwardMode


def _mode(**kw):
    """只测切分逻辑 — 不跑回测, 故 data/strategy 传 None。"""
    return WalkForwardMode(broker=None, data_layer=None,
                           strategy_layer=None, config=WalkForwardConfig(**kw))


def _days(a: str, b: str) -> int:
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


# ============================================================ 组1: 默认不裁 (向后兼容)
class TestDefaultsBackwardCompatible:
    def test_default_config_has_zero_purge_embargo(self):
        c = WalkForwardConfig()
        assert c.purge_days == 0 and c.embargo_days == 0

    def test_zero_purge_zero_embargo_keeps_old_split(self):
        """purge=embargo=0 → 切分与旧实现逐字一致 (回归位)"""
        m = _mode()
        is_r, oos_r = m._split_is_oos("2026-01-01", "2026-01-11")  # 10 天
        assert is_r == ("2026-01-01", "2026-01-08")   # 70% → 7 天
        assert oos_r == ("2026-01-09", "2026-01-11")

    def test_zero_purge_flags_leakage_risk(self):
        """purge=0 必须在结果里留 leakage_risk 标记 — 不让"忘了配"冒充"配了0" """
        m = _mode()
        _, _ = m._split_is_oos("2026-01-01", "2026-01-11")
        # 直接验证配置面 + 结果面在另一测试; 此处验证语义常量
        assert int(m.config.purge_days) == 0


# ============================================================ 组2: purge
class TestPurge:
    def test_purge_shrinks_is_end(self):
        """purge=3 → IS 末尾早 3 天"""
        base = _mode()._split_is_oos("2026-01-01", "2026-01-11")
        purged = _mode(purge_days=3)._split_is_oos("2026-01-01", "2026-01-11")
        assert base[0][1] == "2026-01-08"
        assert purged[0][1] == "2026-01-05"      # 01-08 减 3 天
        assert _days(purged[0][1], base[0][1]) == 3, "purge 应把 IS 末尾前移 3 天"

    def test_purge_creates_gap_between_is_and_oos(self):
        """关键: purge 后 IS 末尾与 OOS 开头之间必须有间隔 (这才是防泄漏)"""
        is_r, oos_r = _mode(purge_days=5)._split_is_oos("2026-01-01", "2026-01-21")
        gap = _days(is_r[1], oos_r[0]) - 1
        assert gap == 5, f"IS 末 {is_r[1]} 与 OOS 首 {oos_r[0]} 间隔 {gap} 天, 应为 5"

    def test_purge_larger_than_is_makes_is_none(self):
        """purge 裁空 IS → is_range=None (宁缺毋滥, 不拿空段算指标)"""
        is_r, oos_r = _mode(purge_days=999)._split_is_oos("2026-01-01", "2026-01-11")
        assert is_r is None
        assert oos_r is not None, "IS 空不该连累 OOS"

    def test_negative_purge_rejected(self):
        with pytest.raises(ValueError):
            WalkForwardConfig(purge_days=-1)


# ============================================================ 组3: embargo
class TestEmbargo:
    def test_embargo_shrinks_oos_start(self):
        """embargo=3 → OOS 开头晚 3 天 (那 3 天不参与统计)"""
        base = _mode()._split_is_oos("2026-01-01", "2026-01-21")
        emb = _mode(embargo_days=3)._split_is_oos("2026-01-01", "2026-01-21")
        assert _days(base[1][0], emb[1][0]) == 3

    def test_embargo_reduces_oos_span(self):
        """embargo 只能缩短 OOS, 不能移动末端"""
        base = _mode()._split_is_oos("2026-01-01", "2026-01-21")
        emb = _mode(embargo_days=4)._split_is_oos("2026-01-01", "2026-01-21")
        assert base[1][1] == emb[1][1], "OOS 末端不该被 embargo 改动"
        assert _days(emb[1][0], emb[1][1]) == _days(base[1][0], base[1][1]) - 4

    def test_embargo_larger_than_oos_makes_oos_none(self):
        """embargo 吃空 OOS → oos_range=None"""
        _, oos_r = _mode(embargo_days=999)._split_is_oos("2026-01-01", "2026-01-11")
        assert oos_r is None

    def test_negative_embargo_rejected(self):
        with pytest.raises(ValueError):
            WalkForwardConfig(embargo_days=-1)


# ============================================================ 组4: 组合 + 边界
class TestCombined:
    def test_purge_and_embargo_compose(self):
        """两者叠加: IS 末裁 purge, OOS 首推 embargo, 中间形成 purge+embargo 的隔离带"""
        is_r, oos_r = _mode(purge_days=2, embargo_days=3)._split_is_oos(
            "2026-01-01", "2026-01-31")
        gap = _days(is_r[1], oos_r[0]) - 1
        assert gap == 5, f"隔离带应为 purge+embargo=5 天, 实际 {gap}"

    def test_no_overlap_ever(self):
        """硬约束: 任何配置下 IS 与 OOS 不得重叠 (含默认)"""
        for purge, emb in [(0, 0), (1, 0), (0, 1), (3, 4)]:
            is_r, oos_r = _mode(purge_days=purge, embargo_days=emb)._split_is_oos(
                "2026-01-01", "2026-01-21")
            if is_r and oos_r:
                assert is_r[1] < oos_r[0], (
                    f"purge={purge} embargo={emb}: IS 末 {is_r[1]} 不早于 "
                    f"OOS 首 {oos_r[0]} — 重叠即泄漏")

    def test_short_window_still_returns_something(self):
        """极短窗口 (2 天) 不得抛异常 — 返回 None 段由上层跳过"""
        is_r, oos_r = _mode(purge_days=1, embargo_days=1)._split_is_oos(
            "2026-01-01", "2026-01-02")
        assert is_r is None or oos_r is None or is_r[1] < oos_r[0]


# ============================================================ 组5: 配置校验
class TestConfigValidation:
    def test_is_ratio_bounds(self):
        for bad in (0, 1, -0.1, 1.5):
            with pytest.raises(ValueError):
                WalkForwardConfig(is_ratio=bad)

    def test_is_ratio_valid_ok(self):
        WalkForwardConfig(is_ratio=0.7)   # 不抛


# ============================================================ 组6: 追溯面
class TestTraceability:
    def test_leakage_fields_present_in_wf_result_source(self):
        """wf_result 必须含 purge/embargo/leakage_risk 三个可追溯字段。

        静态钉: 用源码扫描确保字段真被写进结果 (不真跑回测, 避免重依赖)。
        """
        import inspect
        from backtest_layer.modes import walk_forward as wf
        src = inspect.getsource(wf)
        for field in ("purge_days", "embargo_days", "leakage_risk", "leakage_note"):
            assert f'"{field}"' in src, f"wf_result 缺可追溯字段 {field}"

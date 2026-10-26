# -*- coding: utf-8 -*-
"""W2 卡③ (2026-09-21): quality_check 判定时点 16:20 → 17:50。

病根 (2026-09-20 侦察实锤): 16:20 判定时 DDP 16:00 批只落一半,
detail 记覆盖率 8.7~34.6%<85% fail, 补采后终态 95.4%。09-15/16/17/07/08
五天假阳每晚进 21:07 日报 S5 段 = 狼来了疲劳。

修法 (统筹已批): 判定挪 17:50 —— 17:40 kline 备源补跑 (baostock 17:30 入库,
实测最长 5min) 之后, 保持全系统「判定时点 ≥ 数据到位时点」一个口径
(与 e2e DDP 项读 17:40 后终态同族同修法)。

不挂 depends_on kline: kline 是 cron 直驱非 ops 编排腿, 挂上会引入
假依赖 (参照 concept_plates_daily 16:35 「窗口+错峰」先例, 时点错峰
已足够, 依赖面反而假)。
"""
import pytest


# 判例#8: RED 须 AssertionError 非 ImportError — 模块成员存在性先断言。
def test_config_layer_get_section_exists():
    from config_layer import Config
    assert hasattr(Config, "get_section"), "Config.get_section 不存在 (判例#8)"


def test_quality_check_schedule_is_1750():
    """排程锚: quality_check 须 17:50 (17:40 kline 兜底后)。"""
    from config_layer import Config
    _c = Config()
    _c.load()
    sec = _c.get_section("ops.pipelines") or {}
    qc = sec.get("quality_check") or {}
    assert qc.get("schedule") == "17:50", \
        f"quality_check schedule={qc.get('schedule')} 须=17:50 (16:20 判半采快照)"


def test_quality_check_depends_on_unchanged():
    """depends_on 保持 daily_data_pipeline — 不挂 kline (假依赖反例)。"""
    from config_layer import Config
    _c = Config()
    _c.load()
    sec = _c.get_section("ops.pipelines") or {}
    qc = sec.get("quality_check") or {}
    assert qc.get("depends_on") == ["daily_data_pipeline"], \
        f"depends_on={qc.get('depends_on')} 不得挂 kline (cron 直驱腿, 挂上=假依赖)"


def test_freshness_audit_not_shifted():
    """freshness_audit 保持 16:22 — 卡①已修其 T+1 语义, 时点不动 (修法不扩大)。"""
    from config_layer import Config
    _c = Config()
    _c.load()
    sec = _c.get_section("ops.pipelines") or {}
    fa = sec.get("freshness_audit") or {}
    assert fa.get("schedule") == "16:22", \
        f"freshness_audit schedule={fa.get('schedule')} 须保持 16:22 (T+1 语义已修, 时点无关)"


def test_qc_semantics_comment_present():
    """yaml 须留修法注释 (判定时点≥数据到位时点的口径载体)。"""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                        "config", "defaults", "ops.yaml")
    src = open(os.path.abspath(path), encoding="utf-8").read()
    i = src.find("quality_check:")
    assert i >= 0, "ops.yaml 无 quality_check 块"
    j = src.find("freshness_audit:", i)
    block = src[i:j]
    assert "17:40" in block and "17:50" in block, \
        "quality_check 块须留时点修法注释 (17:40 kline 兜底后 → 17:50)"

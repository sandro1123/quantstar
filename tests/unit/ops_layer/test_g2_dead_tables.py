# -*- coding: utf-8 -*-
"""卡G2 (2026-09-20): popularity_daily/hot_concept_daily 停备 (死表止血)。

用户裁定: 两表是 AdataSentimentPipeline 的一次性种子 (09-12 各 100/20 行),
零消费者、管道零注册零运行 —— incremental_backup 每日在备死数据 = 白做。
处置: 从 INCREMENTAL_TABLES 剔除 (停备), 代码保留不通电不退役
(REC 卡后评估是否作为推荐依据素材通电)。
"""
import pytest


def test_incremental_tables_exclude_dead_pair():
    """备份清单不得含两死表 (停备锚 — 回流即 RED)。"""
    from ops_layer.incremental_backup import INCREMENTAL_TABLES
    names = {t[0] for t in INCREMENTAL_TABLES}
    assert "popularity_daily" not in names, \
        "popularity_daily 已裁定停备 (09-12 死表), 不得回流入增量备份"
    assert "hot_concept_daily" not in names, \
        "hot_concept_daily 已裁定停备 (09-12 死表), 不得回流入增量备份"


def test_dead_tables_marked_in_source():
    """剔除处须有裁定留痕 (勿删代码 — REC 后评估通电)。"""
    import inspect
    from ops_layer import incremental_backup as m
    src = inspect.getsource(m)
    assert "popularity_daily" in src, "源文件须保留裁定注释锚 (勿删)"
    assert "09-20" in src, "裁定日期留痕缺失"


def test_live_tables_unaffected():
    """剔除不误伤 — 活表仍在清单 (抽查四族代表)。"""
    from ops_layer.incremental_backup import INCREMENTAL_TABLES
    names = {t[0] for t in INCREMENTAL_TABLES}
    for t in ("daily_klines", "stock_moneyflow", "pipeline_runs", "stock_news"):
        assert t in names, f"活表 {t} 被误伤剔除"

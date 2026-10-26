"""adata 采集管道 RED 夹具 — 人气榜+热门概念两表 schema+管道 (夜间窗纪律)。

RED: data_layer/pipelines/adata_sentiment_pipeline.py 不存在。
"""
from unittest.mock import MagicMock

import pytest

from data_layer.pipelines.adata_sentiment_pipeline import AdataSentimentPipeline


class TestAdataSentimentPipeline:
    def test_name(self):
        p = AdataSentimentPipeline(db=MagicMock(), source_registry=MagicMock())
        assert p.name == "adata_sentiment"

    def test_run_pulls_both_types_and_writes_two_tables(self):
        db = MagicMock()
        db.get_connection.return_value.execute.return_value.fetchall.return_value = []
        # CREATE TABLE IF NOT EXISTS 走 execute (无返回) — conn 是 MagicMock 全兜
        reg = MagicMock()
        reg.get_best_source.return_value.get_data.side_effect = lambda t, p: (
            [{"code": "600869", "rank": 1, "his_rank": 0.0, "price": None,
              "change_pct": None, "source": "adata"}] if t == "popularity"
            else [{"rank": 1, "concept_code": "885959", "concept_name": "PCB概念",
                   "change_pct": -0.3033, "hot_value": 248880.5,
                   "hot_tag": "连续112天上榜", "source": "adata"}])
        p = AdataSentimentPipeline(db=db, source_registry=reg)
        result = p.run({"date": "2026-09-10"})
        called_types = [c.args[0] for c in reg.get_best_source.return_value.get_data.call_args_list]
        assert "popularity" in called_types
        assert "concept_hot" in called_types
        # 两种写入都发生 (popularity_daily + hot_concept_daily)
        sqls = [c.args[0] for c in db.get_connection.return_value.execute.call_args_list
                if c.args]
        assert any("popularity_daily" in s for s in sqls)
        assert any("hot_concept_daily" in s for s in sqls)

    def test_source_down_degrades_honestly(self):
        # 源不可用: 诚实降级 (空行+日志), 不造假 0
        db = MagicMock()
        reg = MagicMock()
        reg.get_best_source.return_value = None
        p = AdataSentimentPipeline(db=db, source_registry=reg)
        result = p.run({"date": "2026-09-10"})
        assert result["popularity_rows"] == 0
        assert result["concept_hot_rows"] == 0
        assert result.get("degraded") is True

    def test_schema_ddl_registered(self):
        # 两表 DDL 常量在模块级 (施工锁: 列名契约)
        from data_layer.pipelines import adata_sentiment_pipeline as m
        assert "popularity_daily" in m.POPULARITY_DDL
        assert "his_rank" in m.POPULARITY_DDL
        assert "concept_code" in m.HOT_CONCEPT_DDL
        assert "hot_tag" in m.HOT_CONCEPT_DDL

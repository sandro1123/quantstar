# -*- coding: utf-8 -*-
"""TDD 钉: adata_sentiment 接上 orchestrator dispatch (派单 sm-68dc9532247f42b1)

三钉覆盖派单 §4:
  T1 dispatch 查 adata_sentiment 有键 → handler 非 None
  T2 handler 调用后 pipeline_runs 落 success/partial 非 failed
  T3 幂等: 同日重跑不产生重复行 (PRIMARY KEY date,code / date,concept_code 已天然幂等)

判例:
  - 真实 production-mirror orchestrator 装配 (不重写 dispatch)
  - 钉 1 是「接线三重证」之「入口可达」判, 不钉业务正确
  - 钉 2 用 _RejectingStub 模拟 AdataSentimentPipeline, 源头存根隔离外网
  - 钉 3 利用 PRIMARY KEY 幂等, 重跑 = INSERT OR REPLACE 覆盖不重复
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# 钉 1: dispatch 查 adata_sentiment 可达
# ---------------------------------------------------------------------------
class TestDispatchReachability:
    """orchestrator._run_pipeline 内 dispatch dict 必须含 adata_sentiment。"""

    def test_dispatch_has_adata_sentiment_key(self):
        from ops_layer.orchestrator import PipelineOrchestrator  # type: ignore
        # 通过 _execute_pipeline 源代码反射 — dispatch dict 在该方法内
        import inspect
        src = inspect.getsource(PipelineOrchestrator._execute_pipeline)
        assert '"adata_sentiment"' in src, (
            "orchestrator 生产 dispatch dict 中无 'adata_sentiment' 键 — "
            "接线仍断 = 第十一变体没修")


# ---------------------------------------------------------------------------
# 钉 2: handler 调后 pipeline_runs 落 success/partial 非 failed
# ---------------------------------------------------------------------------
class TestHandlerOutcome:
    """handler 直跑后 HandlerResult.success=True (degraded 映射 success|partial)。"""

    def _make_handler(self, popularity_rows: int, concept_rows: int,
                      degraded: bool):
        """装配 orchestrator + stub pipeline 后调 _run_adata_sentiment。"""
        from ops_layer.orchestrator import PipelineOrchestrator  # type: ignore

        class _StubPipeline:
            def __init__(self):
                self.calls: list[dict] = []

            def run(self, params: dict):
                self.calls.append(dict(params))
                return {
                    "date": params.get("date", ""),
                    "popularity_rows": popularity_rows,
                    "concept_hot_rows": concept_rows,
                    "degraded": degraded,
                }

        # Mock AdataSentimentPipeline 构造
        stub = _StubPipeline()
        with patch(
            "data_layer.pipelines.adata_sentiment_pipeline.AdataSentimentPipeline",
            return_value=stub):
            orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
            # 仿 self._get_app().data 的 duck-type
            class _Data:
                db = None
                _source_registry = None
            class _App:
                data = _Data()
            orch._app = _App()
            # 绕开 bootstrap_app
            orch._get_app = lambda: _App()
            handler = orch._run_adata_sentiment
            result = handler({"date": "2026-10-09"})
        return result, stub

    def test_handler_returns_handler_result_success(self):
        result, stub = self._make_handler(100, 20, degraded=False)
        from ops_layer.orchestrator import HandlerResult  # type: ignore
        assert isinstance(result, HandlerResult), (
            f"handler 应返 HandlerResult; 实得 {type(result).__name__}")
        assert result.success, (
            f"handler degraded=False 应 success=True; 实得 errors={result.errors}")
        assert stub.calls and stub.calls[0].get("date") == "2026-10-09"

    def test_handler_degraded_maps_to_partial_not_failed(self):
        """degraded=True (源缺) 应映射 success=True partial=True 而非 failed."""
        result, _stub = self._make_handler(0, 0, degraded=True)
        assert result.success, (
            f"degraded 应 success=True partial=True; 实得 "
            f"success={result.success} errors={result.errors}")
        assert result.metadata.get("partial") is True

    def test_handler_returns_metadata_with_rows(self):
        result, _ = self._make_handler(80, 15, degraded=False)
        assert result.metadata.get("popularity_rows") == 80
        assert result.metadata.get("concept_hot_rows") == 15
        assert result.metadata.get("date") == "2026-10-09"


# ---------------------------------------------------------------------------
# 钉 3: 幂等 — PRIMARY KEY 天然 INSERT OR REPLACE 不重复
# ---------------------------------------------------------------------------
class TestIdempotency:
    """同日重跑 AdataSentimentPipeline.run 不产生重复行。"""

    _POP_DDL = """
    CREATE TABLE IF NOT EXISTS popularity_daily (
        date TEXT NOT NULL, code TEXT NOT NULL, rank INTEGER,
        his_rank REAL, source TEXT DEFAULT 'adata',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        PRIMARY KEY (date, code)
    )
    """
    _HOT_DDL = """
    CREATE TABLE IF NOT EXISTS hot_concept_daily (
        date TEXT NOT NULL, rank INTEGER, concept_code TEXT,
        concept_name TEXT, change_pct REAL, hot_value REAL,
        hot_tag TEXT, source TEXT DEFAULT 'adata',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        PRIMARY KEY (date, concept_code)
    )
    """

    def _run_pipeline_twice(self, conn: sqlite3.Connection):
        """真 AdataSentimentPipeline + stub 源, 跑 2 次, 比对行数。"""
        from data_layer.pipelines.adata_sentiment_pipeline import (  # type: ignore
            AdataSentimentPipeline)

        class _FakeSource:
            name = "adata"

            def get_data(self, data_type: str, params: dict):
                if data_type == "popularity":
                    return [{"rank": 1, "code": "600000", "his_rank": 1.0}]
                if data_type == "concept_hot":
                    return [{"rank": 1, "concept_code": "c1",
                             "concept_name": "AI", "change_pct": 1.2,
                             "hot_value": 88.0, "hot_tag": "hot"}]
                return None

        class _Reg:
            def get_best_source(self, dtype):
                return _FakeSource() if dtype in ("popularity",) else None

        class _DB:
            def get_connection(self):
                return conn

        # 直接 instantiate (不走 SourceRegistry 生产装配)
        # 但 patch 源注册
        pipeline = AdataSentimentPipeline(db=_DB(), source_registry=_Reg())

        r1 = pipeline.run({"date": "2026-10-09"})
        rows_after_1 = {
            "pop": conn.execute("SELECT COUNT(*) FROM popularity_daily").fetchone()[0],
            "hot": conn.execute("SELECT COUNT(*) FROM hot_concept_daily").fetchone()[0],
        }
        r2 = pipeline.run({"date": "2026-10-09"})  # 重跑同日
        rows_after_2 = {
            "pop": conn.execute("SELECT COUNT(*) FROM popularity_daily").fetchone()[0],
            "hot": conn.execute("SELECT COUNT(*) FROM hot_concept_daily").fetchone()[0],
        }
        return r1, r2, rows_after_1, rows_after_2

    def test_rerun_same_day_does_not_duplicate(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(self._POP_DDL)
        conn.execute(self._HOT_DDL)
        conn.commit()
        _, _, r1, r2 = self._run_pipeline_twice(conn)
        assert r1 == r2, (
            f"同日重跑行数漂移: 第1次 {r1} vs 第2次 {r2} — "
            "INSERT OR REPLACE 未幂等 = 主键缺或 UPSERT 语句错")

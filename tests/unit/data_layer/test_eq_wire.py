# -*- coding: utf-8 -*-
"""TDD: easyquotation fallback 实际接线 三刀+反向钉 3P (spec 批 sm-016983fdf52e45cb)

刀1 每源痕迹: _fetch_kline 空窗也打 info (治链内静默)
刀2 越窗诚实: easyquotation 行内 date ≠ end_date → 返回 [] (治假日越窗 PK 撞车)
刀3 假绿根治: kline.run 全空窗 status != 'success' (治 pipeline_runs 假绿)

判例 #150: 反向钉必须跨越对象边界 — P2 用 tmp sqlite 夹具不碰生产库;
P1 实跑 SourceRegistry (带 .env 构型) 钉链序; P3 走真 KlinePipeline 空窗路径。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(str(Path(__file__).resolve().parents[3] / ".env"))

from data_layer.pipelines.kline import KlinePipeline  # noqa: E402
from data_layer.sources.easyquotation_source import (  # noqa: E402
    EasyQuotationSourceAdapter,
)


# ---------------------------------------------------------------------------
# P1 链序钉: 实跑 SourceRegistry (带 .env 生产构型), easyquotation 必在链上
# 且位置 = akshare_sina 后 baostock 前 (spec §4 P1)
# ---------------------------------------------------------------------------
class TestChainOrder:
    def test_eq_in_kline_chain_between_akshare_sina_and_baostock(self):
        from data_layer import DataLayer
        from data_layer.sources.registry import SourceRegistry
        dl = DataLayer.__new__(DataLayer)
        dl._source_registry = SourceRegistry()
        DataLayer._register_sources(dl)
        names = [s.name for s in dl._source_registry.get_sources_for("kline")]
        assert "easyquotation" in names, f"eq 未进 kline 链: {names}"
        eq_i = names.index("easyquotation")
        assert names.index("akshare_sina") < eq_i < names.index("baostock"), (
            f"eq 位置漂移 (应为 akshare_sina 后 baostock 前): {names}")


# ---------------------------------------------------------------------------
# P2 越窗钉 (刀2): 夹具造 sina 快照 — tmp/monkeypatch, 不碰生产库
# 行内 date=09-30 + 请求窗 10-07 → 断言返回 [] (越窗不落库);
# 请求窗 09-30 → 返回 1 行 (窗内正常)。
# ---------------------------------------------------------------------------
class TestOutOfWindowHonestEmpty:
    def _adapter_with_fake_sina(self, fake_row_date: str) -> EasyQuotationSourceAdapter:
        adapter = EasyQuotationSourceAdapter.__new__(EasyQuotationSourceAdapter)
        adapter.force_ipv4 = False
        adapter.easyquotation_failure = {}

        fake_row = {
            "date": fake_row_date, "time": "15:00:00",
            "now": 1258.62, "open": 1250.0, "high": 1260.0, "low": 1249.0,
            "close": 1235.58, "volume": 100, "turnover": 50000,
        }
        adapter._fetch_sina = lambda code: [adapter._to_kline(code, fake_row)]
        return adapter

    def test_holiday_window_row_date_outside_returns_empty(self):
        """假日窗 10-07: sina 行内 date=09-30 越窗 → 诚实返回 [] (刀2 核心)。"""
        adapter = self._adapter_with_fake_sina("2026-09-30")
        out = adapter.get_data("kline", {
            "code": "600519", "start_date": "2026-10-07", "end_date": "2026-10-07"})
        assert out == [], (
            f"越窗行必须诚实返回空 (不落库不撞 PK); 实得 {out}")

    def test_in_window_row_returns_row(self):
        """窗内 (请求窗=date 行内) → 正常返回 1 行 (交易日正常路径不回归)。"""
        adapter = self._adapter_with_fake_sina("2026-10-09")
        out = adapter.get_data("kline", {
            "code": "600519", "start_date": "2026-10-09", "end_date": "2026-10-09"})
        assert isinstance(out, list) and len(out) == 1, f"窗内行应返回; 实得 {out}"
        assert out[0].date == "2026-10-09"


# ---------------------------------------------------------------------------
# P3 假绿钉 (刀3): 逐股全空窗 → status != 'success' 且 errors 非空
# 真 KlinePipeline._fetch_kline 路径, registry 全源 stub 空返回 (不碰生产库/外网)。
# ---------------------------------------------------------------------------
class TestAllEmptyNotSuccess:
    def _pipeline_all_sources_empty(self):
        from data_layer.sources.registry import SourceRegistry

        class _EmptySource:
            name = "stub_empty"
            capabilities = {"kline"}

            def is_available(self):
                return True

            def get_data(self, data_type, params):
                return []

        reg = SourceRegistry()
        reg.register(_EmptySource(), priority=1, type_priorities={"kline": 1})
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE stock_info (code TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1)")
        conn.execute("INSERT INTO stock_info (code) VALUES ('600000')")
        conn.commit()
        return KlinePipeline(source_registry=reg), conn

    def test_all_empty_window_pipeline_not_success(self):
        """全源空窗 → status 不得为 success (假绿根治)。"""
        pipeline, conn = self._pipeline_all_sources_empty()
        from data_layer.pipelines.base import PipelineInput
        inp = PipelineInput(date="2026-10-07", symbols=["600000"], db=conn)
        out = pipeline.run(inp)
        assert out.status != "success", (
            f"全空窗 pipeline_runs 不得假绿; 实得 status={out.status}, "
            f"errors={out.errors}")
        assert out.errors, "空窗必须留痕 errors (判例 #9 载体)"

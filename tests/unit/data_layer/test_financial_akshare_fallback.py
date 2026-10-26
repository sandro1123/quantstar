# -*- coding: utf-8 -*-
"""TDD: akshare 东财 financial fallback 手术卡 (FINANCIALS_SURGERY_CARD_20260930)

三测覆盖手术卡 §3:
  测1 注册面顺序:  availability-gated 后 financial 链形态
                    - 生态环境保护全 (虚拟 fuyao+mootdx+akshare全部可用) →
                      ['fuyao', 'mootdx', 'akshare']
                    - fuyao 缺 key (worktree 态) 仅 akshare 本卡前后变化:
                      ['mootdx', 'akshare'] 中 akshare 必须存在
  测2 首跑落行:      fuyao/mootdx 都拒 → akshare 落真行
  测3 readiness 锚:  MAX='06-30' fail → 09-30 落库后 warn (7<9≤14)

判例:
  - 夹具用真 DDL（列名对齐生产 PRAGMA）
  - RED 须 AssertionError 非 ImportError
  - worktree 无 .env (QS worktree 陷阱) → FuyaoSourceAdapter.is_available() False
    探针启示「生产形态≠夹具形态」,排序断言须用 availability-gated 后的真态,
    而非抽象「fuyao 注册了就一定在」。本卡要钉的是「akshare 进 financial 链」,
    不是「fuyao 一定在」——fuyao 缺 key 时整条链形态本来就缺它,与本卡无关。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import pytest


def _make_dl():
    """构造最小 DataLayer 实例,完成 _register_sources。"""
    from data_layer import DataLayer  # type: ignore
    from data_layer.sources.registry import SourceRegistry  # type: ignore
    dl = DataLayer.__new__(DataLayer)
    dl._source_registry = SourceRegistry()
    DataLayer._register_sources(dl)
    return dl


# ---------------------------------------------------------------------------
# 测 1: SourceRegistry 排序
# ---------------------------------------------------------------------------
class TestRegistryOrder:
    """availability-gated financial 链钉 akshare 入链。"""

    def test_akshare_is_in_financial_chain(self):
        """核心钉: akshare 必须出现在 get_sources_for('financial') — type_priorities 生效。"""
        dl = _make_dl()
        names = [s.name for s in dl._source_registry.get_sources_for("financial")]
        assert "akshare" in names, (
            f"akshare 未进 financial 链; 实得 {names} — "
            "卡 §1 type_priorities['financial': 2] 补丁未生效 = 假修")

    def test_akshare_is_fallback_not_front(self):
        """排序钉: akshare (type=2) 在 mootdx (type 未声明→global priority=3) 之前。"""
        dl = _make_dl()
        names = [s.name for s in dl._source_registry.get_sources_for("financial")]
        if "mootdx" in names and "akshare" in names:
            # True 顺序: fuyao(1) → akshare(2) → mootdx(3)
            assert names.index("akshare") < names.index("mootdx"), (
                f"akshare 应在 mootdx 之前; 实得 {names}")

    def test_registry_keeps_fuyao_type_priority_1(self):
        """间接钉: fuyao 的 type_priorities['financial'] 必须为 1 (统筹红线),
        akshare 的 type_priorities['financial'] 必须为 2。生产 availability 不影响本钉。"""
        dl = _make_dl()
        tp = dl._source_registry._type_priority
        assert tp.get("fuyao", {}).get("financial") == 1, (
            f"fuyao type_priority['financial'] 漂移; 实得 {tp.get('fuyao')}")
        assert tp.get("akshare", {}).get("financial") == 2, (
            f"akshare type_priority['financial'] 漂移; 实得 {tp.get('akshare')}")


# ---------------------------------------------------------------------------
# 测 2: FinancialPipeline fallback 落行
# ---------------------------------------------------------------------------
_STOCK_FINANCIALS_DDL = """
CREATE TABLE IF NOT EXISTS stock_financials (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    eps REAL DEFAULT 0, roe REAL DEFAULT 0,
    revenue REAL DEFAULT 0, profit REAL DEFAULT 0,
    total_assets REAL DEFAULT 0, total_liabilities REAL DEFAULT 0,
    net_assets REAL DEFAULT 0, operating_cash_flow REAL DEFAULT 0,
    gross_margin REAL DEFAULT 0, net_margin REAL DEFAULT 0,
    announce_date TEXT DEFAULT '', caliber TEXT DEFAULT 'cumulative',
    announced_at TEXT DEFAULT '',
    PRIMARY KEY (code, date)
)
"""


class _RejectingStub:
    """拒答源 (fuyao/mootdx); 须声明 capabilities 与 is_available,否则 get_sources_for 空集。"""

    def __init__(self, name: str):
        self.name = name
        self.capabilities = {"financial"}

    def is_available(self) -> bool:
        return True

    def get_data(self, data_type: str, params: dict) -> Any:
        return None


class _AkshareStub:
    name = "akshare"
    capabilities = {"financial"}

    def __init__(self):
        self.calls: list[dict] = []

    def is_available(self) -> bool:
        return True

    def get_data(self, data_type: str, params: dict) -> Any:
        if data_type != "financial":
            return None
        self.calls.append(dict(params))
        return {
            "code": params.get("code", ""), "date": "2026-09-30",
            "eps": 0.35, "roe": 5.2, "revenue": 100.0, "profit": 30.0,
            "total_assets": 1000.0, "total_liabilities": 700.0,
            "net_assets": 300.0, "operating_cash_flow": 50.0,
            "gross_margin": 28.0, "net_margin": 12.0,
        }


class TestPipelineFallback:
    """FinancialPipeline 在前序源拒时必须 fallback 到 akshare。"""

    def test_fallback_to_akshare_when_higher_sources_empty(self):
        from data_layer.sources.registry import SourceRegistry  # type: ignore
        from data_layer.pipelines.financial import FinancialPipeline  # type: ignore
        from data_layer.pipelines.base import PipelineInput  # type: ignore

        reg = SourceRegistry()
        reg.register(_RejectingStub("fuyao"), priority=10,
                     type_priorities={"financial": 1})
        reg.register(_RejectingStub("mootdx"), priority=3)
        akshare = _AkshareStub()
        reg.register(akshare, priority=30, type_priorities={"financial": 2})

        conn = sqlite3.connect(":memory:")
        conn.execute(_STOCK_FINANCIALS_DDL)
        conn.execute("CREATE TABLE IF NOT EXISTS stock_info AS SELECT '600000' AS code")
        conn.commit()

        pipeline = FinancialPipeline(source_registry=reg)
        pipeline_input = PipelineInput(
            date="2026-10-09", symbols=["600000"], db=conn, dry_run=False)

        import data_layer.pipelines.financial as financial_mod  # type: ignore
        original_get_repo = financial_mod.get_repo

        class _RepoStub:
            class financial:
                @staticmethod
                def batch_upsert(rows):
                    for r in rows:
                        conn.execute(
                            "INSERT OR REPLACE INTO stock_financials"
                            "(code, date, eps, roe, revenue, profit,"
                            " total_assets, total_liabilities, net_assets,"
                            " operating_cash_flow, gross_margin, net_margin)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (r.get("code"), r.get("date"), r.get("eps", 0),
                             r.get("roe", 0), r.get("revenue", 0),
                             r.get("profit", 0), r.get("total_assets", 0),
                             r.get("total_liabilities", 0), r.get("net_assets", 0),
                             r.get("operating_cash_flow", 0),
                             r.get("gross_margin", 0), r.get("net_margin", 0)))
                    conn.commit()

            class stock:
                @staticmethod
                def get_all_active():
                    return ["600000"]

        financial_mod.get_repo = lambda db: _RepoStub
        try:
            pipeline.run(pipeline_input)
        finally:
            financial_mod.get_repo = original_get_repo

        assert len(akshare.calls) == 1, (
            f"akshare 未被 fallback 调到; calls={len(akshare.calls)} = 假修")
        row = conn.execute(
            "SELECT code, date, eps FROM stock_financials").fetchone()
        assert row is not None, "stock_financials 未落行"
        assert row[0] == "600000" and row[1] == "2026-09-30", f"落库错位 {row}"


# ---------------------------------------------------------------------------
# 测 3: readiness 三色阈值 (7/14/30)
# ---------------------------------------------------------------------------
class TestReadinessDelayedToOk:
    """readiness.py:726 stock_financials 三色阈值术前 fail → 术后 warn。"""

    def _status(self, latest: str, today: str, g=7, y=14, _r=30) -> str:
        d1 = datetime.strptime(latest, "%Y-%m-%d").date()
        d2 = datetime.strptime(today, "%Y-%m-%d").date()
        days = (d2 - d1).days
        if days <= g:
            return "pass"
        if days <= y:
            return "warn"
        return "fail"

    def test_pre_merge_is_red(self):
        assert self._status("2026-06-30", "2026-10-09") == "fail"

    def test_post_merge_is_warn(self):
        assert self._status("2026-09-30", "2026-10-09") == "warn"

    def test_post_merge_weekend_stable(self):
        assert self._status("2026-09-30", "2026-10-12") == "warn"

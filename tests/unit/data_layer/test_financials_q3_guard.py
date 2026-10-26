# -*- coding: utf-8 -*-
"""financials-q3-window-guard 四钉 TDD (QS 卡号: financials-q3-window-guard)。

四钉 (统筹 GO, 设计稿 Reports/financials_recon_20260930.md 附录六):
  T1 当季披露 → best_report='2026-3' + 批量拉取启动
  T2 当季未披露 → 回落 '2026-2' (现行为零变化)
  T3 覆盖洞 → metadata.missing_by_board 计数显形, 不伪造写入
  T4 源端全拒 → status=failed + errors「权限/配额疑失效」非静默 partial
"""
from unittest.mock import MagicMock, patch

import pytest


# ---------- 件①: _fallback_reports 补当季 ----------

def test_t1_current_quarter_in_list_when_disclosed():
    """T1: 当季在探测列表首位 — 披露后立即可探, 共 5 期。

    锚相对化 (判例=硬日期自然过期): 用今日实时算当季, 不写死 '2026-3'。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from data_layer.sources.fuyao_client import _fallback_reports
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    q = (now.month - 1) // 3 + 1
    cur = f"{now.year}-{q}"
    got = _fallback_reports()
    assert got[0] == cur, f"当季 {cur} 应在首位: {got}"
    assert len(got) == 5, f"5 期含当季: {got}"
    # 前两期 = 当季 + 上季 (连续性)
    assert got[1] == _prev_quarter(cur), f"第二位须上季: {got}"


def _prev_quarter(rpt: str) -> str:
    y, q = rpt.split("-")
    q = int(q)
    if q == 1:
        return f"{int(y) - 1}-4"
    return f"{y}-{q - 1}"


def test_t2_current_quarter_not_disclosed_falls_back():
    """T2: 当季无数据 → _detect_best_report 回落下一位 (现行为零变化)。

    锚相对化 (判例=硬日期自然过期): 实时取 _fallback_reports 首位当季,
    mock 其为拒, 断言回落第二位。
    """
    from data_layer.pipelines import fuyao as fm
    from data_layer.sources.fuyao_client import _fallback_reports

    reports = _fallback_reports()
    cur, prev = reports[0], reports[1]
    calls = []

    def fake_get(path, params):
        calls.append(params.get("report"))
        if params.get("report") == cur:
            return None  # 当季未披露 (源拒/空)
        return {"item": [{"period_end_ms": 1751212800000}]}  # 上季有数据

    client = MagicMock()
    client._get.side_effect = fake_get
    got = fm._detect_best_report(client)
    assert got == prev, f"当季 {cur} 拒须回落 {prev}: {got}"
    assert calls[0] == cur, "先探当季"


# ---------- 件②: 覆盖洞显形 ----------

def test_t3_missing_by_board_metadata():
    """T3: 北交所 missing 计数进 metadata, 不伪造写入。"""
    from data_layer.pipelines.fuyao import FuyaoPipeline
    from data_layer.pipelines.base import PipelineInput

    pipe = FuyaoPipeline()
    pipe._client = MagicMock()
    pipe.client.api_key = "k"
    pipe.client.available.return_value = True
    # 源端: 当季拒, 上季有数据 (probe 成功→target_period)
    pipe.client._get.side_effect = lambda p, params: (
        None if params.get("report") == "2026-3"
        else {"item": [{"period_end_ms": 1782096000000}]})
    # 逐股四类拉取全 5003 拒 → indicators 空 → indicators_missing 计数
    pipe.client.get_income_statements.return_value = []
    pipe.client.get_balance_sheets.return_value = []
    pipe.client.get_cash_flow.return_value = []
    pipe.client.get_financial_indicators.return_value = {}

    repo = MagicMock()
    # 550 只沪深已覆盖 + 268 只北交所缺 (43/83/87/92)
    repo.stock.get_all_active.return_value = (
        ["600%03d" % i for i in range(550)]
        + ["430017", "830002", "870003", "920001"] * 67
    )
    repo.financial.codes_for_date.return_value = {"600%03d" % i for i in range(550)}
    with patch("data_layer.pipelines.fuyao.get_repo", return_value=repo),          patch("data_layer.pipelines.fuyao._report_period_date",
               return_value="2026-06-30"):
        out = pipe.run(PipelineInput(date="2026-10-02"))

    md = out.metadata or {}
    assert md.get("indicators_missing", 0) >= 268, f"北交所 indicators 缺须计数: {md}"
    assert "missing_by_board" in md, f"覆盖洞须显形 (新键): {md}"
    bj = sum(v for k, v in md["missing_by_board"].items()
             if k in ("43", "83", "87", "92"))
    assert bj >= 268, f"北交所计数应 >=268: {md.get('missing_by_board')}"


# ---------- 件④: 源端全拒显形 ----------

def test_t4_all_endpoints_rejected_failed_not_partial():
    """T4: 四端点全拒 → status=failed + errors「权限/配额疑失效」非静默 partial。"""
    from data_layer.pipelines.fuyao import FuyaoPipeline
    from data_layer.pipelines.base import PipelineInput

    pipe = FuyaoPipeline()
    pipe._client = MagicMock()
    pipe.client.api_key = "k"
    pipe.client.available.return_value = True
    pipe.client._get.return_value = None  # 探针四端点全拒 (1002)

    repo = MagicMock()
    repo.stock.get_all_active.return_value = ["600519"]
    with patch("data_layer.pipelines.fuyao.get_repo", return_value=repo):
        out = pipe.run(PipelineInput(date="2026-10-02"))

    assert out.status == "failed", f"全拒须 failed 非 partial: {out.status}"
    assert any(("权限" in e or "配额" in e or "全拒" in e) for e in out.errors),         f"errors 须显性「权限/配额疑失效」: {out.errors}"


# ---------- 件③: 季表 freshness 窗口判 ----------

def test_t5_fiscal_window_recent_quarter_ok():
    """T5: 季表 06-30 行在披露窗内 → ok 非 delayed (双哨兵口径合一)。"""
    from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
    # 2026-09-30 审计, actual=2026-06-30 (Q2 季末) — 距 92 天但 Q3 披露未至窗口
    # 窗口锚 = Q2 季末 06-30 + 45 天 = 08-14 已过? 注意: 窗口从**最近季末**起算。
    # 09-30 时最近已过季末=09-30 (Q3), actual=06-30 非 Q3 末 → 窗口判 False?
    # 实际语义: actual 是 Q2 末, Q2 的披露窗 = 06-30+45=08-14 — 09-30 已超。
    # 但 Q3 报告期行本就该 10 月才来 — 表级滞后是语义而非断血。
    # 件③的判据锚 = 「actual 是否为最近一个应有报告期」: 09-30 时应有报告期
    # 仍是 Q2 (Q3 未到披露窗), 故 ok。窗口判用「actual 所在季的下一季披露窗
    # 起点」: Q2 下一披露窗起点 = 08-31 (H1 集中披露止), 判 09-30 在 Q3 窗前
    # → Q2 行仍是「最新应有」→ ok。
    f = FreshnessAuditPipeline._fiscal_window_ok
    # Q3 未到: actual=Q2 末 (06-30), audit=09-30 → Q3 季末 09-30 当日, 判窗口
    # 内 (Q3 披露窗 10 月才开) → True
    assert f("2026-09-30", "2026-06-30") is True


def test_t6_fiscal_window_stale_beyond_window():
    """T6: 披露窗已过仍无新报告期 → delayed (真断血显形)。"""
    from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
    f = FreshnessAuditPipeline._fiscal_window_ok
    # audit=2026-11-20, actual=2026-06-30: Q3 披露窗 (10 月) 已过而表还停在
    # Q2 → False → delayed 显形
    assert f("2026-11-20", "2026-06-30") is False

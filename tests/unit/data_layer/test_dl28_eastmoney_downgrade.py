"""DL-28 (D-43): 东财系 akshare 端点应用层反爬记档 + 软降级标记 — 验收测试

验收三点:
1. 记档名单是单一事实源 (eastmoney_downgrade.py), 7 个东财端点全在册,
   非东财端点 (kline/财务主链) 不在册。
2. 软降级标记: 东财端点失败 → em_downgrades 打标 (D-43 + 原因 + 错误 + 时间);
   成功 → 清标 (哪天通了自动恢复, 无需改代码)。
3. 不屏蔽调用: 失败仍走原有兜底路径 (limit_up 返回 [] 等), 行为不变。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root / "src"))

from data_layer.eastmoney_downgrade import (
    DOWNGRADE_DECISION,
    DOWNGRADE_REASON,
    EASTMONEY_ENDPOINTS,
    is_eastmoney_endpoint,
)
from data_layer.sources.akshare_source import AkShareSourceAdapter

# ---------------------------------------------------------------------------
# 记档名单 (单一事实源)
# ---------------------------------------------------------------------------

def test_eastmoney_endpoints_has_7_entries():
    assert len(EASTMONEY_ENDPOINTS) == 7

def test_eastmoney_endpoints_content():
    expected = {
        "stock_value_em", "stock_zh_a_spot_em", "stock_zh_a_hist_min_em",
        "stock_zt_pool_em", "stock_zt_pool_dtgc_em", "stock_research_report_em",
        "stock_sector_fund_flow_rank",
    }
    assert EASTMONEY_ENDPOINTS == expected

def test_non_eastmoney_endpoints_not_downgraded():
    """kline/财务主链不带 _em 且未被判定受东财反爬影响, 不得误标。"""
    for ep in ("stock_zh_a_hist", "stock_financial_analysis_indicator",
               "stock_zh_a_daily", "stock_info_a_code_name"):
        assert not is_eastmoney_endpoint(ep)

def test_decision_and_reason_recorded():
    assert DOWNGRADE_DECISION == "D-43"
    assert "应用层反爬" in DOWNGRADE_REASON

# ---------------------------------------------------------------------------
# 软降级标记: 失败打标 / 成功清标
# ---------------------------------------------------------------------------

def test_em_call_failure_marks_downgrade():
    adapter = AkShareSourceAdapter()
    ak = MagicMock()
    ak.stock_zt_pool_em.side_effect = ConnectionError("RemoteDisconnected")
    with pytest.raises(ConnectionError):
        adapter._em_call(ak, "stock_zt_pool_em", date="20260821")
    assert "stock_zt_pool_em" in adapter.em_downgrades
    mark = adapter.em_downgrades["stock_zt_pool_em"]
    assert mark["decision"] == "D-43"
    assert "应用层反爬" in mark["reason"]
    assert "RemoteDisconnected" in mark["error"]
    assert mark["ts"]

def test_em_call_success_clears_mark():
    """哪天通了自动恢复: 成功调用清掉降级标记, 无需改代码 (D-43 原文)。"""
    adapter = AkShareSourceAdapter()
    adapter.em_downgrades["stock_zt_pool_em"] = {
        "decision": "D-43", "reason": "x", "error": "old", "ts": "old",
    }
    ak = MagicMock()
    ak.stock_zt_pool_em.return_value = "df"
    assert adapter._em_call(ak, "stock_zt_pool_em", date="20260821") == "df"
    assert "stock_zt_pool_em" not in adapter.em_downgrades

def test_em_call_non_eastmoney_failure_not_marked():
    """非东财端点失败不打降级标 (仍正常抛异常)。"""
    adapter = AkShareSourceAdapter()
    ak = MagicMock()
    ak.stock_zh_a_hist.side_effect = ConnectionError("boom")
    with pytest.raises(ConnectionError):
        adapter._em_call(ak, "stock_zh_a_hist", symbol="000001")
    assert adapter.em_downgrades == {}

# ---------------------------------------------------------------------------
# 不屏蔽调用: 原有兜底路径行为不变
# ---------------------------------------------------------------------------

def test_limit_up_pool_failure_still_returns_empty():
    """东财涨停池失败 → 仍返回 [] (registry 会继续 fallback 到下一源),
    同时打上降级标记。"""
    adapter = AkShareSourceAdapter()
    ak = MagicMock()
    ak.stock_zt_pool_em.side_effect = ConnectionError("blocked")
    assert adapter._get_limit_up_pool(ak, {"date": "2026-08-21"}) == []
    assert "stock_zt_pool_em" in adapter.em_downgrades

def test_limit_down_pool_failure_still_returns_empty():
    adapter = AkShareSourceAdapter()
    ak = MagicMock()
    ak.stock_zt_pool_dtgc_em.side_effect = ConnectionError("blocked")
    assert adapter._get_limit_down_pool(ak, {"date": "2026-08-21"}) == []
    assert "stock_zt_pool_dtgc_em" in adapter.em_downgrades

def _concept_df():
    # #116 面二 swap: 10jqka 列形状 (探针 2026-09-05 01:37 实测)
    import pandas as pd
    return pd.DataFrame({
        "序号": [1], "行业": ["AI应用"], "行业-涨跌幅": [1.5],
        "行业指数": [1000.0], "流入资金": [2.0], "流出资金": [1.0],
        "净额": [1.0], "公司家数": [10], "领涨股": ["000001"],
        "领涨股-涨跌幅": [3.3], "当前价": [10.0],
    })

def test_sector_fund_flow_partial_failure_keeps_good_half():
    """行业流失败/概念流成功 → 只丢失败的一半, 成功的一半照收。

    #116 面二 swap 后端点为 10jqka 系 (非东财) — 不再走 _em_call, 无降级标
    语义; 本测保留「半失败照收好半」的行为验收 (重试结构 + 软降级不抛)。
    """
    adapter = AkShareSourceAdapter()
    ak = MagicMock()

    ak.stock_fund_flow_industry.side_effect = ConnectionError("blocked")
    ak.stock_fund_flow_concept.return_value = _concept_df()

    records = adapter._get_sector_fund_flow(ak, {})
    assert len(records) == 1
    assert records[0]["sector_type"] == "concept"
    assert records[0]["sector_name"] == "AI应用"
    # 10jqka 端点不走 _em_call → 不涉降级标记 (东财族专属机制)
    assert "stock_fund_flow_concept" not in adapter.em_downgrades

def test_sector_fund_flow_total_failure_marks_downgrade():
    """行业+概念全失败 → 软降级返回空列表, 不抛 (10jqka 通道同款行为验收)。

    #116 面二 swap: 原降级标记断言随端点迁移作废 — 10jqka 端点非东财族,
    无 em_downgrades 打标; 行为验收 = 空列表 + 两个扇面各重试 1 次。
    """
    adapter = AkShareSourceAdapter()
    ak = MagicMock()
    ak.stock_fund_flow_industry.side_effect = ConnectionError("blocked")
    ak.stock_fund_flow_concept.side_effect = ConnectionError("blocked")
    records = adapter._get_sector_fund_flow(ak, {})
    assert records == []
    assert adapter.em_downgrades == {}  # 10jqka 非东财族: 不打标
    assert ak.stock_fund_flow_industry.call_count == 2  # 重试 1 次结构保留
    assert ak.stock_fund_flow_concept.call_count == 2

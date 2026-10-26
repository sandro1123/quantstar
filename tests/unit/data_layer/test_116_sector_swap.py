"""#116 面二 sector_fund_flow 10jqka swap — 施工验收测试 (E)

swap 内容 (v2 记档文案口径):
  东财端点 ak.stock_sector_fund_flow_rank(indicator="今日") 08-22 起被应用层
  反爬 (D/43 已知降级不绕过), 且只供当日快照 → 结构性不可回补。
  修法 = 换通道不换架构: 切到同花顺 10jqka 通道
  ak.stock_fund_flow_industry / ak.stock_fund_flow_concept(symbol="即时")。

字段映射 (探针 2026-09-05 01:37 实测列名):
  直映 5: sector_name=行业 / sector_type=industry|concept / change_pct=行业-涨跌幅
         / main_net_amount=净额 / top_stock=领涨股
  置 NULL 5: main_net_ratio / super_large_amount / large_amount
            / medium_amount / small_amount
  (10jqka 不供占比与大小单拆分 — 宁 NULL 不假值, #122 判例族: 置 NULL 前
   消费面已审计, 五字段全仓零算术消费者, writer _safe_float(None)→NULL 构造性安全)
  不映射 4: 公司家数/行业指数/领涨股-涨跌幅/当前价 (10jqka 独有, 不扩表零架构变更)
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root / "src"))

from data_layer.sources.akshare_source import AkShareSourceAdapter


def _industry_df():
    return pd.DataFrame([
        {"序号": 1, "行业": "养殖业", "行业指数": 3043.89, "行业-涨跌幅": 4.36,
         "流入资金": 54.33, "流出资金": 38.31, "净额": 16.02, "公司家数": 36,
         "领涨股": "罗牛山", "领涨股-涨跌幅": 10.05, "当前价": 6.13},
    ])


def _concept_df():
    return pd.DataFrame([
        {"序号": 1, "行业": "猪肉", "行业指数": 2569.52, "行业-涨跌幅": 5.89,
         "流入资金": 69.56, "流出资金": 48.67, "净额": 20.89, "公司家数": 32,
         "领涨股": "大禹生物", "领涨股-涨跌幅": 17.7, "当前价": 7.58},
    ])


# --- swap 后: 调用面 (新端点 + symbol="即时" 参数) ---

def test_swap_calls_10jqka_endpoints():
    """swap 后必须调用 10jqka 两端点 (industry + concept), 不再走东财端点。"""
    adapter = AkShareSourceAdapter()
    ak = MagicMock()
    ak.stock_fund_flow_industry.return_value = _industry_df()
    ak.stock_fund_flow_concept.return_value = _concept_df()

    records = adapter._get_sector_fund_flow(ak, {})

    ak.stock_fund_flow_industry.assert_called_once_with(symbol="即时")
    ak.stock_fund_flow_concept.assert_called_once_with(symbol="即时")
    ak.stock_sector_fund_flow_rank.assert_not_called()


def test_swap_maps_five_direct_fields():
    """五直映字段: 行业→sector_name, 涨跌幅→change_pct, 净额→main_net_amount,
    领涨股→top_stock; 两扇面 label industry/concept 照旧。"""
    adapter = AkShareSourceAdapter()
    ak = MagicMock()
    ak.stock_fund_flow_industry.return_value = _industry_df()
    ak.stock_fund_flow_concept.return_value = _concept_df()

    records = adapter._get_sector_fund_flow(ak, {})

    assert len(records) == 2
    by_type = {r["sector_type"]: r for r in records}
    assert by_type["industry"]["sector_name"] == "养殖业"
    assert by_type["industry"]["change_pct"] == 4.36
    assert by_type["industry"]["main_net_amount"] == 16.02
    assert by_type["industry"]["top_stock"] == "罗牛山"
    assert by_type["concept"]["sector_name"] == "猪肉"
    assert by_type["concept"]["main_net_amount"] == 20.89


def test_swap_nulls_five_unsupported_fields():
    """10jqka 不供占比/大小单拆分 → 五字段必须为 None (宁 NULL 不假值,
    不拿净额硬造占比、不把净额拆分冒充大小单)。"""
    adapter = AkShareSourceAdapter()
    ak = MagicMock()
    ak.stock_fund_flow_industry.return_value = _industry_df()
    ak.stock_fund_flow_concept.return_value = _concept_df()

    records = adapter._get_sector_fund_flow(ak, {})

    for r in records:
        for k in ("main_net_ratio", "super_large_amount", "large_amount",
                  "medium_amount", "small_amount"):
            assert r[k] is None, f"{k} 应为 None (宁 NULL 不假值), got {r[k]!r}"


def test_swap_cross_check_net_amount():
    """净额 = 流入资金 − 流出资金 交叉验证口径 (探针 head 数据自洽)。"""
    adapter = AkShareSourceAdapter()
    ak = MagicMock()
    ak.stock_fund_flow_industry.return_value = _industry_df()
    ak.stock_fund_flow_concept.return_value = _concept_df()

    records = adapter._get_sector_fund_flow(ak, {})
    for r in records:
        df = _industry_df() if r["sector_type"] == "industry" else _concept_df()
        expected = round(float(df.iloc[0]["流入资金"]) - float(df.iloc[0]["流出资金"]), 2)
        assert abs(r["main_net_amount"] - expected) < 0.01


# --- swap 后: 重试结构保留 (间歇失败重试 1 次) ---

def test_swap_partial_failure_keeps_good_half():
    """行业失败/概念成功 → 只丢失败一半; 重试结构保留。"""
    adapter = AkShareSourceAdapter()
    ak = MagicMock()

    def _side_effect(symbol):
        raise ConnectionError("blocked")

    ak.stock_fund_flow_industry.side_effect = _side_effect
    ak.stock_fund_flow_concept.return_value = _concept_df()

    records = adapter._get_sector_fund_flow(ak, {})
    assert len(records) == 1
    assert records[0]["sector_type"] == "concept"
    assert ak.stock_fund_flow_industry.call_count == 2  # 重试 1 次


def test_swap_total_failure_returns_empty():
    """行业+概念全失败 → 空列表 (软降级, 不抛)。"""
    adapter = AkShareSourceAdapter()
    ak = MagicMock()
    ak.stock_fund_flow_industry.side_effect = ConnectionError("blocked")
    ak.stock_fund_flow_concept.side_effect = ConnectionError("blocked")

    records = adapter._get_sector_fund_flow(ak, {})
    assert records == []
    assert ak.stock_fund_flow_industry.call_count == 2
    assert ak.stock_fund_flow_concept.call_count == 2

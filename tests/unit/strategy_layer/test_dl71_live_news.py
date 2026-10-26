"""DL-71: 个股新闻按需取测试 (统筹 sm-b3254cb457254ab1 裁定方向)

根因 (DL-66 普查): stock_news 三个采集源全是宏观新闻流, code 恒空串,
get_stock_news(code,...) 个股分支恒返回空 → 深度研判新闻维度一直瞎。
修复: 不做每日全量采集, 改按需取 —
  - data_layer.get_stock_news_live(code): 现场按股拉 akshare stock_news_em, 不落库
  - deep_review._fetch_stock_news: 库无个股新闻时经读 API 现场取
  - 策略层不直连 akshare (DL-41 边界保持)

验收点:
1. 库空 → 触发 get_stock_news_live, 返回新闻摘要。
2. 库有个股新闻 (code 精确匹配) → 用库数据, 不触发按需取。
3. 库只有市场新闻 (code='') → 不算个股新闻, 触发按需取。
4. 按需取失败 → 诚实返回空, 不阻断。
5. data_layer=None → 返回空 (不触发)。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root / "src"))

from strategy_layer.agent_gate.deep_review import _fetch_stock_news


def _dl(db_news=None, live_news=None, live_raises=False):
    dl = MagicMock()
    dl.get_stock_news.return_value = db_news or []
    if live_raises:
        dl.get_stock_news_live.side_effect = RuntimeError("network down")
    else:
        dl.get_stock_news_live.return_value = live_news or []
    return dl


def test_db_empty_triggers_live_fetch():
    """DL-71: 库恒空 → 现场按股拉新闻, 返回摘要。"""
    dl = _dl(db_news=[], live_news=[
        {"title": "公司中标大单", "code": "600519"},
        {"title": "业绩预增", "code": "600519"},
    ])
    summary = _fetch_stock_news("600519", "2026-08-23", dl)
    assert summary.startswith("个股新闻: ")
    assert "公司中标大单" in summary
    assert "业绩预增" in summary
    dl.get_stock_news_live.assert_called_once()


def test_db_has_stock_news_skips_live():
    """库有个股新闻 (code 精确匹配) → 用库数据, 不触发按需取。"""
    dl = _dl(db_news=[{"title": "库内个股新闻", "code": "600519"}],
             live_news=[{"title": "不应出现"}])
    summary = _fetch_stock_news("600519", "2026-08-23", dl)
    assert "库内个股新闻" in summary
    assert "不应出现" not in summary
    dl.get_stock_news_live.assert_not_called()


def test_market_news_not_counted_triggers_live():
    """库只有市场新闻 (code='') → 不算个股新闻, 触发按需取。"""
    dl = _dl(db_news=[{"title": "市场宏观新闻", "code": ""}],
             live_news=[{"title": "现场个股新闻", "code": "600519"}])
    summary = _fetch_stock_news("600519", "2026-08-23", dl)
    assert "现场个股新闻" in summary
    assert "市场宏观新闻" not in summary
    dl.get_stock_news_live.assert_called_once()


def test_live_fetch_failure_returns_empty():
    """按需取失败 → 诚实返回空, 不造假值不抛异常。"""
    dl = _dl(db_news=[], live_raises=True)
    assert _fetch_stock_news("600519", "2026-08-23", dl) == ""


def test_no_data_layer_returns_empty():
    """data_layer=None → 返回空 (不触发任何取数)。"""
    assert _fetch_stock_news("600519", "2026-08-23", None) == ""


def test_legacy_items_without_code_field_still_match():
    """DL-41 前向兼容: 无 code 字段的项视为匹配 (旧调用方)。"""
    dl = _dl(db_news=[{"title": "旧格式新闻"}])  # 无 code 字段
    summary = _fetch_stock_news("600519", "2026-08-23", dl)
    assert "旧格式新闻" in summary
    dl.get_stock_news_live.assert_not_called()

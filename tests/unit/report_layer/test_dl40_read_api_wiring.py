"""DL-40: 报告层直连收编 — 5 个 section 全部经 DataLayer 读 API 取数。

验收:
1. 5 个 section 不再直接实例化 FuyaoClient/GTimgSourceAdapter/NewsCollector
2. data_layer 为 None 或读 API 返回空时诚实降级 (显示"数据不可用"/空列表), 不崩溃不造假值
3. 新增读 API get_limit_pool / get_sector_rotation 走 SourceRegistry

红-绿: 撤回 DL-40 补丁时本文件必须失败。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from report_layer.sections.indices import IndicesSection, fetch_realtime_indices
from report_layer.sections.midday_alerts import MiddayAlertsSection
from report_layer.sections.midday_news import MiddayNewsSection
from report_layer.sections.midday_sectors import MiddaySectorsSection
from report_layer.sections.news import NewsSection


def _ctx(data_layer=None):
    return SimpleNamespace(data_layer=data_layer, date="2026-08-22",
                           llm_service=None)


class _FakeDL:
    """模拟 DataLayer 读 API."""
    def __init__(self, **returns):
        self._returns = returns
        self.calls = []

    def get_intraday_news(self, date, limit=8, **kw):
        self.calls.append(("get_intraday_news", date))
        return self._returns.get("news", [])

    def get_realtime_indices(self, codes=None):
        self.calls.append(("get_realtime_indices",))
        return self._returns.get("indices", [])

    def get_limit_pool(self, date):
        self.calls.append(("get_limit_pool", date))
        return self._returns.get("limit_pool", {"up": [], "down": []})

    def get_sector_rotation(self, date=""):
        self.calls.append(("get_sector_rotation",))
        return self._returns.get("sectors", [])


# --- news.py ---

def test_news_uses_read_api():
    dl = _FakeDL(news=[{"title": "测试新闻", "summary": "摘要", "sentiment": "bullish"}])
    result = NewsSection().build(_ctx(dl))
    assert "get_intraday_news" in [c[0] for c in dl.calls]
    assert "测试新闻" in result.body

def test_news_empty_honest_degrade():
    dl = _FakeDL(news=[])
    result = NewsSection().build(_ctx(dl))
    assert "暂无重要新闻" in result.body

def test_news_no_data_layer():
    result = NewsSection().build(_ctx(None))
    assert "暂无重要新闻" in result.body


# --- midday_news.py ---

def test_midday_news_uses_read_api():
    dl = _FakeDL(news=[{"title": "午间新闻", "summary": "内容"}])
    result = MiddayNewsSection().build(_ctx(dl))
    assert "get_intraday_news" in [c[0] for c in dl.calls]
    assert "午间新闻" in result.body

def test_midday_news_empty():
    dl = _FakeDL(news=[])
    result = MiddayNewsSection().build(_ctx(dl))
    assert "暂无重要新闻" in result.body


# --- indices.py ---

def test_indices_uses_read_api():
    dl = _FakeDL(indices=[{"name": "上证指数", "price": 3200.5, "change_pct": 0.85}])
    result = IndicesSection().build(_ctx(dl))
    assert "get_realtime_indices" in [c[0] for c in dl.calls]
    assert "上证指数" in result.body

def test_indices_empty_honest_degrade():
    dl = _FakeDL(indices=[])
    result = IndicesSection().build(_ctx(dl))
    assert "暂不可用" in result.body

def test_fetch_realtime_indices_no_dl():
    assert fetch_realtime_indices(None) == []


# --- midday_alerts.py ---

def test_midday_alerts_uses_read_api():
    pool = {"up": [{"name": "测试股", "code": "000001", "count": 3, "change_pct": 10.0}],
            "down": []}
    dl = _FakeDL(limit_pool=pool)
    result = MiddayAlertsSection().build(_ctx(dl))
    assert "get_limit_pool" in [c[0] for c in dl.calls]
    assert "测试股" in result.body
    assert "3连板" in result.body

def test_midday_alerts_empty():
    dl = _FakeDL(limit_pool={"up": [], "down": []})
    result = MiddayAlertsSection().build(_ctx(dl))
    assert "暂无涨跌停数据" in result.body

def test_midday_alerts_no_data_layer():
    result = MiddayAlertsSection().build(_ctx(None))
    assert "暂无涨跌停数据" in result.body


# --- midday_sectors.py ---

def test_midday_sectors_uses_read_api():
    dl = _FakeDL(sectors=[{"name": "半导体", "pct": 2.5}, {"name": "白酒", "pct": -1.2}])
    result = MiddaySectorsSection().build(_ctx(dl))
    assert "get_sector_rotation" in [c[0] for c in dl.calls]
    assert "半导体" in result.body

def test_midday_sectors_empty():
    dl = _FakeDL(sectors=[])
    result = MiddaySectorsSection().build(_ctx(dl))
    assert "暂无板块数据" in result.body


# --- 源码守卫: 不允许直连回潮 ---

def test_no_direct_source_client_in_report_sections():
    """守卫: report_layer/sections/ 不得直接实例化源客户端 (DL-40 防复发)。"""
    import pathlib
    sections_dir = pathlib.Path(__file__).resolve().parents[3] / "src" / "report_layer" / "sections"
    violations = []
    for py in sections_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for pattern in ("FuyaoClient()", "GTimgSourceAdapter()", "NewsCollector("):
            if pattern in text:
                violations.append(f"{py.name}: {pattern}")
    assert not violations, f"报告层仍有直连源客户端: {violations}"

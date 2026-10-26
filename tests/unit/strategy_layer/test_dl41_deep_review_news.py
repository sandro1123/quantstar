"""DL-41: deep_review 切 get_stock_news — 策略层不再直连外部数据源。

红-绿: 撤回 DL-41 补丁时本文件必须失败。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategy_layer.agent_gate.deep_review import DeepReview, _fetch_stock_news


class _FakeDL:
    def __init__(self, news=None):
        self._news = news or []
        self.calls = []

    def get_stock_news(self, code, date, limit=20):
        self.calls.append((code, date))
        return self._news


def test_fetch_stock_news_uses_read_api():
    dl = _FakeDL(news=[{"title": "利好公告"}, {"title": "业绩预增"}])
    result = _fetch_stock_news("600519", "2026-08-22", dl)
    assert dl.calls == [("600519", "2026-08-22")]
    assert "利好公告" in result
    assert "业绩预增" in result

def test_fetch_stock_news_empty_honest():
    dl = _FakeDL(news=[])
    assert _fetch_stock_news("600519", "2026-08-22", dl) == ""

def test_fetch_stock_news_no_data_layer():
    assert _fetch_stock_news("600519", "2026-08-22", None) == ""

def test_fetch_stock_news_exception_honest():
    dl = MagicMock()
    dl.get_stock_news.side_effect = Exception("db error")
    assert _fetch_stock_news("600519", "2026-08-22", dl) == ""

def test_deep_review_threads_data_layer():
    dl = _FakeDL(news=[{"title": "测试新闻"}])
    dr = DeepReview(agent_layer=None, data_layer=dl)
    assert dr.data_layer is dl

def test_deep_review_default_no_data_layer():
    dr = DeepReview(agent_layer=None)
    assert dr.data_layer is None


# --- 源码守卫: strategy_layer 不得 import akshare (DL-41 防复发) ---

def test_no_akshare_in_strategy_layer():
    import pathlib
    strategy_dir = pathlib.Path(__file__).resolve().parents[3] / "src" / "strategy_layer"
    violations = []
    for py in strategy_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "import akshare" in text or "akshare." in text:
            violations.append(str(py.relative_to(strategy_dir)))
    assert not violations, f"strategy_layer 仍有 akshare 直连: {violations}"

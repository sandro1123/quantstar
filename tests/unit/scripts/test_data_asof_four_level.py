# -*- coding: utf-8 -*-
"""data_asof 四级降级链 TDD (09-28 统筹裁 c 两案合一, sm-6025654604c345fe)。

7c8c9004 只到 daily_klines 一级, 「数据到但价没到」场景失真 (09-25 数据到
但价停更) — last_price_date 提到最高优先级。b6121b6e 撤 (缺 added_date 兜底)。
四级: last_price_date→daily_klines→added_date→unknown。
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "marimo_pages"))


def _asof():
    import _common
    return _common.data_asof()


def _router(last_price, klines, added):
    def fake_one(sql, params=()):
        if "last_price_date" in sql:
            return (last_price,)
        if "daily_klines" in sql:
            return (klines,)
        if "added_date" in sql:
            return (added,)
        return (None,)
    return fake_one


# 钉①: 一级 last_price_date 优先 (真吃到价才算)
def test_level1_last_price_date_first():
    with patch("_common.one", side_effect=_router("2026-09-25", "2026-09-24", "2026-09-23")):
        assert _asof() == "2026-09-25"


# 钉②: 二级降级 daily_klines (价停更但数据到)
def test_level2_fallback_daily_klines():
    with patch("_common.one", side_effect=_router(None, "2026-09-24", "2026-09-23")):
        assert _asof() == "2026-09-24"


# 钉③: 三级兜底 added_date (价数据皆空, 业务日兜底)
def test_level3_fallback_added_date():
    with patch("_common.one", side_effect=_router(None, None, "2026-09-23")):
        assert _asof() == "2026-09-23"


# 钉④: 四级 unknown (三锚皆无, 宁 unknown 不假锚) + 空行边界
def test_level4_unknown_and_empty_row():
    with patch("_common.one", side_effect=_router(None, None, None)):
        assert _asof() == "unknown"
    # 空行 (one 返回 None 而非 (None,)) 也 unknown
    with patch("_common.one", return_value=None):
        assert _asof() == "unknown"

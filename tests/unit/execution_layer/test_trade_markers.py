"""Tests for trade marker data generation (G-E11)"""
from unittest.mock import MagicMock

from execution_layer.analysis.trade_markers import TradeMarker, TradeMarkerSet, TradeMarkerGenerator


def test_trade_marker_dataclass():
    marker = TradeMarker(code="000001", date="2026-07-01", marker_type="entry", price=15.0, reason="策略推荐")
    assert marker.code == "000001"
    assert marker.marker_type == "entry"
    assert marker.reason == "策略推荐"


def test_trade_marker_default_reason():
    marker = TradeMarker(code="000001", date="2026-07-01", marker_type="entry", price=15.0)
    assert marker.reason == ""


def test_trade_marker_set_dataclass():
    markers = [
        TradeMarker(code="000001", date="2026-07-01", marker_type="entry", price=15.0),
        TradeMarker(code="000001", date="2026-07-15", marker_type="exit", price=18.0),
    ]
    marker_set = TradeMarkerSet(code="000001", markers=markers)
    assert marker_set.code == "000001"
    assert len(marker_set.markers) == 2


def test_trade_marker_set_default():
    marker_set = TradeMarkerSet(code="000001")
    assert marker_set.markers == []


def test_trade_markers_entry_exit():
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "entry_date": "2026-07-01", "entry_price": 15.0,
        "exit_date": "2026-07-15", "exit_price": 18.0,
        "entry_reason": "策略推荐", "exit_reason": "止盈",
        "stop_loss": 13.0, "take_profit": 20.0,
    }
    generator = TradeMarkerGenerator(pool, MagicMock())
    markers = generator.get_markers("000001")
    assert len(markers.markers) == 4
    assert markers.markers[0].marker_type == "entry"
    assert markers.markers[-1].marker_type == "exit"


def test_trade_markers_entry_only():
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "entry_date": "2026-07-01", "entry_price": 15.0,
        "entry_reason": "策略推荐",
    }
    generator = TradeMarkerGenerator(pool, MagicMock())
    markers = generator.get_markers("000001")
    assert len(markers.markers) == 1
    assert markers.markers[0].marker_type == "entry"


def test_trade_markers_nonexistent_stock():
    pool = MagicMock()
    pool.get.return_value = None
    generator = TradeMarkerGenerator(pool, MagicMock())
    markers = generator.get_markers("NONEXIST")
    assert len(markers.markers) == 0
    assert markers.code == "NONEXIST"


def test_trade_markers_sorted_by_date():
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "entry_date": "2026-07-10", "entry_price": 15.0,
        "exit_date": "2026-07-01", "exit_price": 18.0,
        "entry_reason": "策略推荐", "exit_reason": "止盈",
        "stop_loss": 13.0, "take_profit": 20.0,
    }
    generator = TradeMarkerGenerator(pool, MagicMock())
    markers = generator.get_markers("000001")
    assert markers.markers[0].date == "2026-07-01"
    assert markers.markers[-1].date == "2026-07-10"


def test_trade_markers_no_optional_fields():
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "entry_date": "2026-07-01", "entry_price": 15.0,
    }
    generator = TradeMarkerGenerator(pool, MagicMock())
    markers = generator.get_markers("000001")
    assert len(markers.markers) == 1
    assert markers.markers[0].reason == ""


def test_trade_markers_stop_loss_reason():
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "entry_date": "2026-07-01", "entry_price": 15.0,
        "stop_loss": 13.0,
    }
    generator = TradeMarkerGenerator(pool, MagicMock())
    markers = generator.get_markers("000001")
    sl_markers = [m for m in markers.markers if m.marker_type == "stop_loss"]
    assert len(sl_markers) == 1
    assert sl_markers[0].reason == "止损线"


def test_trade_markers_take_profit_reason():
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "entry_date": "2026-07-01", "entry_price": 15.0,
        "take_profit": 20.0,
    }
    generator = TradeMarkerGenerator(pool, MagicMock())
    markers = generator.get_markers("000001")
    tp_markers = [m for m in markers.markers if m.marker_type == "take_profit"]
    assert len(tp_markers) == 1
    assert tp_markers[0].reason == "止盈线"


def test_trade_markers_no_entry_date_for_sl_tp():
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "entry_price": 15.0,
        "stop_loss": 13.0, "take_profit": 20.0,
    }
    generator = TradeMarkerGenerator(pool, MagicMock())
    markers = generator.get_markers("000001")
    for m in markers.markers:
        if m.marker_type in ("stop_loss", "take_profit"):
            assert m.date == ""


def test_trade_markers_partial_missing():
    pool = MagicMock()
    pool.get.return_value = {
        "code": "000001", "entry_date": "2026-07-01", "entry_price": 15.0,
        "exit_date": "2026-07-15",
    }
    generator = TradeMarkerGenerator(pool, MagicMock())
    markers = generator.get_markers("000001")
    assert len(markers.markers) == 1
    assert markers.markers[0].marker_type == "entry"
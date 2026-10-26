"""DL-04 验收测试 — get_risky_codes fail-closed + 选股阻断

fail-open (旧): 查询失败返回空集 → ST/退市安全闸整个打开。
fail-closed (新): 查询失败返回 None (区别于空集), 调用方必须中止选股并告警。
当日缓存兜底: 查询失败但当日有成功结果时用缓存+告警。
"""
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from data_layer._loader import DataLoader


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _make_loader(tmp_path, with_table=True):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    if with_table:
        conn.execute("""
            CREATE TABLE stock_info (
                code TEXT PRIMARY KEY, name TEXT, is_active INTEGER DEFAULT 1
            )
        """)
        conn.execute("INSERT INTO stock_info VALUES ('000001', '平安银行', 1)")
        conn.execute("INSERT INTO stock_info VALUES ('000430', '*ST张家界', 1)")
        conn.execute("INSERT INTO stock_info VALUES ('600001', '立方退', 0)")
        conn.commit()
    return DataLoader(_FakeDB(conn))


def test_get_risky_codes_returns_set_on_success(tmp_path):
    loader = _make_loader(tmp_path)
    codes = loader.get_risky_codes()
    assert isinstance(codes, set)
    assert "000430" in codes  # *ST
    assert "600001" in codes  # 退 + is_active=0
    assert "000001" not in codes


def test_get_risky_codes_returns_none_on_db_error_no_cache(tmp_path):
    """验收: mock db 异常 → 返回 None (非空集), 无当日缓存"""
    loader = _make_loader(tmp_path)

    def _boom():
        raise sqlite3.OperationalError("database is locked")

    loader.db.get_connection = _boom
    result = loader.get_risky_codes()
    assert result is None, "查询失败且无缓存应返回 None (fail-closed), 不是空集"


def test_get_risky_codes_same_day_cache_fallback(tmp_path):
    """查询失败但当日有成功缓存 → 返回缓存 + 不返回 None"""
    loader = _make_loader(tmp_path)
    # 先成功一次, 建立当日缓存
    first = loader.get_risky_codes()
    assert isinstance(first, set) and first
    # 再让查询失败
    def _boom():
        raise sqlite3.OperationalError("database is locked")
    loader.db.get_connection = _boom
    result = loader.get_risky_codes()
    assert result == first, "当日缓存兜底应返回缓存结果"
    assert result is not None


def test_get_risky_codes_stale_cache_not_used(tmp_path):
    """缓存是昨天的 → 查询失败时不用过期缓存, 返回 None"""
    loader = _make_loader(tmp_path)
    first = loader.get_risky_codes()
    assert isinstance(first, set)
    # 把缓存日期改成昨天
    loader._risky_cache = ("1999-01-01", first)
    def _boom():
        raise sqlite3.OperationalError("database is locked")
    loader.db.get_connection = _boom
    result = loader.get_risky_codes()
    assert result is None, "过期缓存不应兜底"


def test_strategy_run_daily_aborts_when_risky_codes_none():
    """验收: 策略层收到 None → 中止本轮选股, selection=None, 不产出候选。

    让 aggregate 返回空列表, 使因子/IC/ML 等中间步骤全为 no-op,
    只聚焦验证 get_risky_codes=None 时 run_daily 早退、selector 不被调用。
    """
    from strategy_layer import StrategyLayer

    layer = StrategyLayer.__new__(StrategyLayer)  # 跳过重型 __init__
    layer.config = None
    layer.mode = "live"
    layer.db = None
    layer._state = {}

    # mock data_layer: snapshot 给股票列表, get_risky_codes 返回 None (fail-closed)
    fake_dl = MagicMock()
    fake_kline = MagicMock()
    fake_kline.code = "000001"
    fake_kline.close = 10.0
    fake_dl.snapshot.return_value = MagicMock(klines=[fake_kline])
    fake_dl.get_risky_codes.return_value = None
    layer.data_layer = fake_dl

    # mock 重型组件; aggregate 返回 [] → 后续 IC/ML/持久化循环全为 no-op
    layer.factor_engine = MagicMock()
    layer.factor_engine.compute_all.return_value = MagicMock()
    layer.aggregator = MagicMock()
    layer.aggregator.aggregate.return_value = []
    layer.aggregator.detect_redundant_factors.return_value = None
    layer.aggregator.fill_recent_returns.return_value = 0
    layer.selector = MagicMock()
    layer.signal_generator = MagicMock()
    layer.deep_review = MagicMock()

    with patch.object(StrategyLayer, "market_regime", return_value="neutral"), \
         patch("strategy_layer.feedback.persistence.WeightPersistence") as wp_cls, \
         patch("strategy_layer.ALL_FACTORS", [object()]):
        wp_cls.return_value.load_weights.return_value = None
        result = layer.run_daily("2026-08-18")

    assert result.selection is None, "get_risky_codes=None 应中止选股"
    assert result.signals == []
    layer.selector.run.assert_not_called()

"""DL-39-exec (D-66): factor_daily 31 物理列正式弃用 — 验收测试

验收两点:
1. cache.py Layer-2 对 deprecated 列跳过 DB 查询 (不再白查必 miss 的空缓存)。
2. 弃用名单是单一事实源, 新鲜度审计豁免名单 == 弃用名单 (挂 DL-29/DL-38 声明式机制,
   不造第二套)。

实测前提 (生产库只读, 2026-08-23): 31 列全部存在于 schema; xueqiu_heat_score/
ps_ttm/momentum_20d/momentum_score 非空行数=0; 其余各 600~690 万历史行保留。
"""
import sqlite3

import pytest

from data_layer.deprecated_columns import (
    DEPRECATED_FACTOR_COLUMNS,
    DEPRECATED_SINCE,
    is_deprecated_factor_column,
)
from strategy_layer.cache import FactorCache
from strategy_layer.factors.base import FactorResult


@pytest.fixture(autouse=True)
def _reset_valid_columns_cache():
    """cache.py 的 _VALID_COLUMNS 是模块级全局, 测试间必须复位防污染。"""
    import strategy_layer.cache as cache_mod
    cache_mod._VALID_COLUMNS = None
    yield
    cache_mod._VALID_COLUMNS = None


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _make_db_with_factor_daily():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE factor_daily (date TEXT, code TEXT, pe_ttm REAL, rsi_14 REAL, "
        "fwd_return_5d REAL, factor_scores TEXT)"
    )
    # 一行历史数据: deprecated 列 pe_ttm 有值, 活列 fwd_return_5d 有值
    conn.execute(
        "INSERT INTO factor_daily VALUES ('2026-07-16', '000001', 12.5, 55.0, 1.23, '{}')"
    )
    conn.commit()
    return _FakeDB(conn)


# ---------------------------------------------------------------------------
# 弃用名单本身
# ---------------------------------------------------------------------------

def test_deprecated_set_has_31_columns():
    # DL-66 普查补录 ma_status 后为 32 列 (原 DL-39 31 列 + ma_status)。
    assert len(DEPRECATED_FACTOR_COLUMNS) == 32


def test_fwd_return_not_deprecated():
    """IC 环唯一输入 fwd_return_* 必须活着, 不在弃用范围。"""
    for c in ("fwd_return_1d", "fwd_return_5d", "fwd_return_20d"):
        assert c not in DEPRECATED_FACTOR_COLUMNS
        assert not is_deprecated_factor_column(c)


def test_deprecated_since_is_0717():
    assert DEPRECATED_SINCE == "2026-07-17"


def test_is_deprecated_factor_column():
    assert is_deprecated_factor_column("pe_ttm")
    assert is_deprecated_factor_column("concept_count")
    assert not is_deprecated_factor_column("factor_scores")
    assert not is_deprecated_factor_column("date")


# ---------------------------------------------------------------------------
# cache.py Layer-2 跳过 deprecated 列
# ---------------------------------------------------------------------------

class _SpyConnection:
    """包装 sqlite3.Connection, 记录 execute 的 SQL (sqlite3.Connection.execute 只读, 不能直接 monkey-patch)。"""

    def __init__(self, conn):
        self._conn = conn
        self.executed = []

    def execute(self, sql, *a, **k):
        self.executed.append(sql)
        return self._conn.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_cache_skips_db_for_deprecated_column():
    """deprecated 列 (pe_ttm) 即使 DB 有历史值也不查 Layer-2, 直接 miss 落现算。"""
    db = _make_db_with_factor_daily()
    spy = _SpyConnection(db.get_connection())
    cache = FactorCache(db=_FakeDB(spy))

    result = cache.get("000001", "2026-07-16", "pe_ttm")
    # deprecated → Layer-2 跳过 → 无 DB 查询 → miss
    assert result is None
    assert not any("factor_daily" in s for s in spy.executed), (
        f"deprecated 列不应触发 Layer-2 查询, 实际: {spy.executed}"
    )
    assert cache._stats["db_hit"] == 0


def test_cache_still_queries_live_column():
    """活列 (fwd_return_5d) 照常走 Layer-2 命中。"""
    db = _make_db_with_factor_daily()
    cache = FactorCache(db=db)
    result = cache.get("000001", "2026-07-16", "fwd_return_5d")
    assert result is not None
    assert result.raw_value == 1.23
    assert cache._stats["db_hit"] == 1


def test_cache_deprecated_column_no_db_still_safe():
    """无 DB 时 deprecated 列也安全返回 miss (不崩)。"""
    cache = FactorCache(db=None)
    assert cache.get("000001", "2026-07-16", "pe_ttm") is None


# ---------------------------------------------------------------------------
# 新鲜度审计豁免名单 == 弃用名单 (单一事实源, 不造第二套)
# ---------------------------------------------------------------------------

def test_freshness_exempt_equals_deprecated_set():
    """豁免名单复用弃用名单 (挂 DL-29/DL-38 声明式机制)。"""
    from data_layer.deprecated_columns import FRESHNESS_EXEMPT_FACTOR_COLUMNS

    assert FRESHNESS_EXEMPT_FACTOR_COLUMNS == DEPRECATED_FACTOR_COLUMNS


def test_freshness_audit_does_not_flag_deprecated_columns():
    """freshness_audit 是表级审计, factor_daily 不在其审计清单 → deprecated 列
    不会被报为缺陷。此测试钉住: 若未来给 factor_daily 加列级新鲜度检查,
    必须先查 FRESHNESS_EXEMPT_FACTOR_COLUMNS 豁免, 否则此测试提醒补豁免。"""
    from data_layer.health.readiness import DataReadiness

    audited = set(DataReadiness.REQUIRED_TABLES) | set(DataReadiness.COLLECT_TABLES)
    # factor_daily 既非必需表也非冷采集表 → 表级审计不碰它的列
    assert "factor_daily" not in audited

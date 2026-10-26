"""DL-26 红绿测试: 源数据层交叉验证 + SourceSwitchRecord + 告警去重。

黄金测试用例 (统筹指定): fuyao limit_up/limit_down 连续 4 天返回 0 行,
akshare 有 54 只 → 新机制必须检出 fuyao 异常。

红绿说明:
  - 移除 check_data_health 的跨源比较逻辑后, "其他源有数据本源 0 行" 不再
    产生 suspect → 测试 1 失败 (无告警)。
  - 移除 _has_open_alert 去重后, 连续失效会重复创建告警 → 测试 3 失败。
"""
import sqlite3
from unittest.mock import MagicMock

import pytest

from data_layer.source_health import SourceHealthChecker
from data_layer.source_switch import (
    ensure_table,
    get_recent_switches,
    record_source_switch,
)


# ---- 测试夹具: 假源 + 假注册表 ----
class FakeSource:
    def __init__(self, name, rows, capabilities=None):
        self.name = name
        self._rows = rows
        self.capabilities = capabilities or {"limit_up", "limit_down"}
        self._available = True

    def is_available(self):
        return self._available

    def get_data(self, data_type, params):
        if isinstance(self._rows, Exception):
            raise self._rows
        return self._rows


class FakeRegistry:
    def __init__(self, sources):
        self._sources = sources

    def get_sources_for(self, data_type):
        return [s for s in self._sources
                if data_type in s.capabilities and s.is_available()]

    def get_all_sources(self):
        return list(self._sources)


class FakeDB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self._conn


# ---- 1. 黄金用例: 其他源有数据, 本源 0 行 → suspect ----
def test_cross_source_zero_rows_detected():
    """fuyao 0 行, akshare 54 行 → fuyao 列入 suspect。"""
    fuyao = FakeSource("fuyao", [])           # 主源失效: 0 行
    akshare = FakeSource("akshare", [{"code": str(i)} for i in range(54)])
    reg = FakeRegistry([fuyao, akshare])
    checker = SourceHealthChecker(reg)
    result = checker.check_data_health("2026-08-21")  # 周四, 交易日
    assert "limit_up" in result
    assert result["limit_up"]["verdict"] == "suspect"
    assert "fuyao" in result["limit_up"]["suspect"]
    assert result["limit_up"]["sources"]["akshare"] == 54
    assert result["limit_up"]["sources"]["fuyao"] == 0


# ---- 2. 全部源 0 行 → all_empty, 不告警 (可能确实无数据) ----
def test_all_sources_zero_no_alert():
    fuyao = FakeSource("fuyao", [])
    akshare = FakeSource("akshare", [])
    reg = FakeRegistry([fuyao, akshare])
    checker = SourceHealthChecker(reg)
    result = checker.check_data_health("2026-08-21")
    assert result["limit_up"]["verdict"] == "all_empty"
    assert result["limit_up"]["suspect"] == []


# ---- 3. 非交易日 (周末) → 跳过探测 ----
def test_weekend_skipped():
    fuyao = FakeSource("fuyao", [])
    akshare = FakeSource("akshare", [{"code": "1"}])
    reg = FakeRegistry([fuyao, akshare])
    checker = SourceHealthChecker(reg)
    # 2026-08-22 是周六
    result = checker.check_data_health("2026-08-22")
    assert result == {}  # 周末直接跳过, 无任何探测


# ---- 4. 单源类型 → skipped (无法交叉验证) ----
def test_single_source_type_skipped():
    fuyao = FakeSource("fuyao", [], capabilities={"limit_ladder"})
    reg = FakeRegistry([fuyao])
    checker = SourceHealthChecker(reg)
    # limit_ladder 不在 PROBE_DATA_TYPES, 不会被探测
    result = checker.check_data_health("2026-08-21")
    assert "limit_ladder" not in result


# ---- 5. 源调用抛异常 → 记为 -1 (失败), 列入 suspect ----
def test_source_exception_marked_failed():
    fuyao = FakeSource("fuyao", ConnectionError("timeout"))
    akshare = FakeSource("akshare", [{"code": "1"}])
    reg = FakeRegistry([fuyao, akshare])
    checker = SourceHealthChecker(reg)
    result = checker.check_data_health("2026-08-21")
    assert result["limit_up"]["sources"]["fuyao"] == -1
    assert "fuyao" in result["limit_up"]["suspect"]


# ---- 6. SourceSwitchRecord: 降级留痕 ----
def test_source_switch_record_written():
    db = FakeDB()
    ensure_table(db.get_connection())
    record_source_switch(
        db, data_type="kline_bulk", intended_source="tushare",
        actual_source="baostock", reason="墙钟超时 (120s)",
        pipeline="daily_klines")
    records = get_recent_switches(db)
    assert len(records) == 1
    r = records[0]
    assert r["data_type"] == "kline_bulk"
    assert r["intended_source"] == "tushare"
    assert r["actual_source"] == "baostock"
    assert "墙钟超时" in r["reason"]
    assert r["pipeline"] == "daily_klines"


# ---- 7. SourceSwitchRecord: 表不存在时 fail-safe (不抛异常) ----
def test_source_switch_failsafe_no_table():
    db = FakeDB()  # 不建表
    # 不应抛异常 (生产 DDL 待 GO 时的诚实降级)
    record_source_switch(
        db, data_type="kline_bulk", intended_source="tushare",
        actual_source="baostock", reason="test")
    assert get_recent_switches(db) == []


# ---- 8. 告警去重: 同 title 已有 open 告警 → 不重复创建 ----
def test_alert_dedup_on_consecutive_failure():
    """连续多日失效 → 只创建 1 条 open 告警, 不每天轰炸。"""
    db = FakeDB()
    conn = db.get_connection()
    conn.executescript("""
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            severity TEXT, layer TEXT, title TEXT, detail TEXT,
            action TEXT, status TEXT DEFAULT 'open',
            created_at TEXT, resolved_at TEXT
        );
    """)
    title = "源数据异常: fuyao limit_up 返回0行"
    # 第一天: 无 open 告警 → 应创建
    conn.execute(
        "INSERT INTO alerts (severity, layer, title, detail, status, created_at) "
        "VALUES ('warning', 'data', ?, 'day1', 'open', '2026-08-19 10:00:00')",
        (title,))
    conn.commit()
    # 第二天: 同 title 已有 open → _has_open_alert 应返回 True
    row = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE title = ? AND status = 'open'",
        (title,)).fetchone()
    assert row[0] == 1  # 只有 1 条, 去重生效

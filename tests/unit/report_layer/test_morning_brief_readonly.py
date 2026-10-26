"""晨报入口 dry-run: 使用真实临时 SQLite，原库不写、不采集、不调用 LLM。"""
import sqlite3
from unittest.mock import MagicMock

import pytest

import scripts.build_morning_brief as mb
from common.database import Database
from execution_layer.tables import STOCK_POOL_DDL


@pytest.fixture
def source(monkeypatch, tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as conn:
        conn.execute(STOCK_POOL_DDL)
        conn.execute("INSERT INTO stock_pool (code, name, status, added_date, entry_price, position_qty, last_price) VALUES ('000001', '测试持仓', 'active', '2026-09-16', 10, 100, 11)")
        conn.execute("CREATE TABLE stock_news (code TEXT, date TEXT, title TEXT, source TEXT, sentiment TEXT, sentiment_score REAL, summary TEXT)")
        conn.execute("INSERT INTO stock_news VALUES ('', ?, '缓存新闻', 'fixture', 'neutral', 50, '缓存摘要')", (mb.DATE,))
        conn.execute("CREATE TABLE factor_daily (code TEXT, date TEXT, factor_scores TEXT)")
        conn.execute("CREATE TABLE model_predictions (code TEXT, date TEXT, blended_score REAL, ml_score REAL)")
        conn.execute("CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT)")
        conn.execute("INSERT INTO factor_daily VALUES ('000001', ?, '{\"momentum_20d\":0.5}')", (mb.DATE,))
        conn.execute("INSERT INTO model_predictions VALUES ('000001', ?, 0.8, 0.7)", (mb.DATE,))
        conn.execute("INSERT INTO stock_info VALUES ('000001', '测试持仓', '测试行业')")
    monkeypatch.setenv("QS_TEST_DB", str(path))
    return path


def test_cli_dry_run_preserves_source_and_uses_cached_data(source, monkeypatch, tmp_path, capsys):
    before = source.read_bytes()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mb.sys, "argv", ["build_morning_brief.py", "--dry-run"])
    monkeypatch.setattr(mb, "build_overnight", lambda: {})
    monkeypatch.setattr(mb, "build_indices", lambda: [{"name": "测试指数", "price": 1000, "pct": 1}])
    forbidden = {name: MagicMock(side_effect=AssertionError(name)) for name in
                 ("NewsCollector", "_init_llm", "DeliveryLayer", "DataLayer", "StrategyLayer")}
    for name, mock in forbidden.items():
        monkeypatch.setattr(mb, name, mock)
    updates = MagicMock(side_effect=AssertionError("daily_update"))
    monkeypatch.setattr(mb.ExecutionLayer, "daily_update", updates)
    writes = []
    original_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        conn = original_connect(database, *args, **kwargs)
        if str(database) != ":memory:":
            def authorizer(action, arg1, arg2, dbname, trigger):
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE,
                              sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_CREATE_INDEX}:
                    writes.append((action, arg1))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorizer)
        return conn

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    mb.main()
    output = capsys.readouterr().out
    assert "缓存新闻" in output
    assert "综合评分: 0.800" in output
    assert "组合净值: 300,100.00" in output
    for mock in forbidden.values():
        mock.assert_not_called()
    updates.assert_not_called()
    assert writes == []
    assert source.read_bytes() == before
    assert (tmp_path / "output" / "preview" / f"morning_brief_{mb.DATE}.txt").exists()


def test_missing_readonly_source_is_not_created(monkeypatch, tmp_path):
    path = tmp_path / "absent.db"
    monkeypatch.setenv("QS_TEST_DB", str(path))
    assert mb.build_positions(dry_run=True) is None
    assert not path.exists()

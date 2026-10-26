"""#116 面一 freshness_sentinel 09:30 轻哨 — 验收测试 (mock + :memory:, 无网络)"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))            # scripts/ 直驱形态
sys.path.insert(0, str(_root / "src"))    # src 包导入形态

import scripts.freshness_sentinel as fs


class _FakeDB:
    """:memory: 库 + 只建 freshness_audit (哨兵只读此表)。"""
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE freshness_audit (date TEXT, table_name TEXT, "
            "expected_by TEXT, actual_at TEXT, delay_min INTEGER, status TEXT, "
            "created_at TEXT)")
        self.conn.commit()

    def rows_for(self, date: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM freshness_audit WHERE date=?", (date,))]

    def add(self, date: str, table: str, status: str, delay=None):
        self.conn.execute(
            "INSERT INTO freshness_audit VALUES (?, ?, ?, ?, ?, ?, '2026-01-01 09:00')",
            (date, table, date, date if status == "ok" else None, delay, status))
        self.conn.commit()


@pytest.fixture
def audit(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(fs, "read_audit",
                        lambda d: db.rows_for(d))
    return db


def _with_collect_tables(d: str) -> int:
    """哨兵日期: 读前一日审计行。"""
    return fs.run(d, notify_disabled=True)


# --- 日期口径 ---

def test_prev_date_simple():
    assert fs._prev_trading_date("2026-09-07") == "2026-09-06"


# --- 判读面 ---

def test_all_ok_no_alert(audit, capsys):
    d = "2026-09-07"
    for t in ("sector_fund_flow", "lhb_daily"):
        audit.add("2026-09-06", t, "ok")
    rc = _with_collect_tables(d)
    assert rc == 0
    assert "all collect tables ok" in capsys.readouterr().out


def test_missing_table_alerts(audit, capsys):
    """lhb_daily 前日 missing (零行日) → rc=1 + ALERT 行 — #116 面一主场景。"""
    d = "2026-09-07"
    audit.add("2026-09-06", "sector_fund_flow", "ok")
    audit.add("2026-09-06", "lhb_daily", "missing")
    rc = _with_collect_tables(d)
    assert rc == 1
    out = capsys.readouterr().out
    assert "1 not-ok" in out
    assert "ALERT" in out


def test_delayed_table_alerts(audit, capsys):
    d = "2026-09-07"
    audit.add("2026-09-06", "sector_fund_flow", "delayed", delay=1440)
    rc = _with_collect_tables(d)
    assert rc == 1


def test_no_audit_rows_gentle_not_alert(audit, capsys):
    """前日无审计行 (管线未跑) → rc=0 轻提示非告警 (诚实形态)。"""
    rc = _with_collect_tables("2026-09-07")
    assert rc == 0
    assert "no audit rows" in capsys.readouterr().out


# --- 推送面 ---

def test_send_alert_best_effort_silent(capsys):
    """delivery 导入/发送失败 → 静默降级不抛 (哨兵不能因告警失败而哑)。"""
    import builtins
    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if name.startswith("config_layer"):
            raise ImportError("no cfg")
        return real_import(name, *a, **k)

    sys.modules.pop("config_layer", None)
    # monkeypatch builtins.__import__ via pytest monkeypatch fixture style
    import unittest.mock as um
    with um.patch("builtins.__import__", side_effect=_boom):
        fs.send_alert("t", "b")  # 不抛 = 过
    assert "静默降级" in capsys.readouterr().err


def test_run_notifies_by_default(monkeypatch, capsys):
    """默认形态会调 send_alert (rc 语义与 --no-notify 一致)。"""
    db = _FakeDB()
    db.add("2026-09-06", "lhb_daily", "missing")
    monkeypatch.setattr(fs, "read_audit", lambda d: db.rows_for(d))
    sent = []
    monkeypatch.setattr(fs, "send_alert", lambda t, b: sent.append((t, b)))
    rc = fs.run("2026-09-07", notify_disabled=False)
    assert rc == 1
    assert sent and "lhb_daily" in sent[0][1]


# --- 集成面: COLLECT_TABLES 清单含 lhb_daily (#116 面一) ---

def test_collect_tables_includes_lhb():
    from data_layer.health.readiness import DataReadiness
    # W2 卡① (2026-09-20): lhb_daily/index_daily 是 T+1 源, 已移 T1_COLLECT_TABLES
    # (当日清单按当日判 = #115 每日假阳的病根, 旧断言把病锁住了)。
    # 原意图保留: 两表必须被 freshness 审计覆盖 —— 现由 T1 注册表承载。
    assert "lhb_daily" in DataReadiness.T1_COLLECT_TABLES
    assert DataReadiness.T1_COLLECT_TABLES["lhb_daily"] == "date"
    assert "index_daily" in DataReadiness.T1_COLLECT_TABLES
    assert DataReadiness.T1_COLLECT_TABLES["index_daily"] == "date"
    # 反向锚: T+1 源不得回流当日清单 (否则假阳复发)
    assert "lhb_daily" not in DataReadiness.COLLECT_TABLES
    assert "index_daily" not in DataReadiness.COLLECT_TABLES
    assert len(DataReadiness.COLLECT_TABLES) == 8
    assert len(DataReadiness.T1_COLLECT_TABLES) == 2

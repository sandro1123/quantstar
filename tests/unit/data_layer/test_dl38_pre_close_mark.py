"""DL-38 验收测试 — pre_close=0 不可回补标记 + 质检豁免

实测分类 (生产库 2026-08-22): 5969 行全部为该股在库首行, 0 行有前序 close>0
(物理不可回补)。成因: ipo_day 3606 / collection_start 2107 / no_list_date 256。
标记复用 DL-29 data_quality_flags (flag='unrecoverable', rule='pre_close_unrecoverable'),
不 ALTER daily_klines, 不造第二套机制。
"""
import sqlite3
import subprocess
import sys
from pathlib import Path

from common.database import Database
from data_layer.quality import KlineQualityGate, check_kline_batch


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _make_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    return _FakeDB(conn)


def _rec(code, date, pre_close=1.0, close=1.0):
    return {"code": code, "date": date, "open": close, "high": close, "low": close,
            "close": close, "volume": 100.0, "amount": 100.0,
            "pre_close": pre_close, "change_pct": 0.0, "turnover_rate": 0.0}


# ---------------------------------------------------------------------------
# 分类逻辑 (mark 脚本 classify)
# ---------------------------------------------------------------------------

def _seed_db(tmp_path):
    """造一个含三类 pre_close=0 的小库。"""
    db_path = tmp_path / "seed.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE daily_klines (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL, pre_close REAL, change_pct REAL,
            turnover_rate REAL, PRIMARY KEY (code, date))
    """)
    conn.execute("CREATE TABLE stock_info (code TEXT, name TEXT, list_date TEXT)")
    # ipo_day: 首行 == 上市日
    conn.execute("INSERT INTO daily_klines (code, date, close, pre_close) "
                 "VALUES ('300001', '2026-05-18', 10.0, 0)")
    conn.execute("INSERT INTO stock_info VALUES ('300001', 'IPO股', '20260518')")
    # collection_start: 首行但上市更早
    conn.execute("INSERT INTO daily_klines (code, date, close, pre_close) "
                 "VALUES ('600001', '2026-05-18', 10.0, 0)")
    conn.execute("INSERT INTO stock_info VALUES ('600001', '老股', '20100101')")
    # no_list_date: 无 list_date
    conn.execute("INSERT INTO daily_klines (code, date, close, pre_close) "
                 "VALUES ('830001', '2026-06-29', 10.0, 0)")
    conn.execute("INSERT INTO stock_info VALUES ('830001', '北交所', '')")
    conn.commit()
    conn.close()
    return db_path


def test_classify_three_causes(tmp_path):
    db_path = _seed_db(tmp_path)
    script = Path(__file__).resolve().parents[3] / "scripts" / "mark_pre_close_unrecoverable.py"
    r = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--dry-run"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ipo_day: 1" in r.stdout
    assert "collection_start: 1" in r.stdout
    assert "no_list_date: 1" in r.stdout
    assert "可回补漏网行数 (应为 0): 0" in r.stdout


def test_mark_apply_writes_flags(tmp_path):
    db_path = _seed_db(tmp_path)
    script = Path(__file__).resolve().parents[3] / "scripts" / "mark_pre_close_unrecoverable.py"
    r = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--apply",
         "--evidence", str(tmp_path / "ev.jsonl")],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT code, flag, rule FROM data_quality_flags "
        "WHERE rule='pre_close_unrecoverable' ORDER BY code").fetchall()
    assert len(rows) == 3
    assert all(r["flag"] == "unrecoverable" for r in rows)
    codes = {r["code"] for r in rows}
    assert codes == {"300001", "600001", "830001"}
    conn.close()


def test_mark_aborts_if_backfillable_leak(tmp_path):
    """存在可回补行 (有前序 close>0) → 中止, 不全标 unrecoverable"""
    db_path = tmp_path / "leak.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE daily_klines (
            code TEXT, date TEXT, close REAL, pre_close REAL, PRIMARY KEY (code, date))
    """)
    conn.execute("CREATE TABLE stock_info (code TEXT, name TEXT, list_date TEXT)")
    # 该股有前序 close>0, 但第二行 pre_close=0 → 其实可回补, 是漏网
    conn.execute("INSERT INTO daily_klines VALUES ('600002', '2026-05-17', 9.0, 8.0)")
    conn.execute("INSERT INTO daily_klines VALUES ('600002', '2026-05-18', 10.0, 0)")
    conn.execute("INSERT INTO stock_info VALUES ('600002', 'x', '20100101')")
    conn.commit()
    conn.close()
    script = Path(__file__).resolve().parents[3] / "scripts" / "mark_pre_close_unrecoverable.py"
    r = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--apply"],
        capture_output=True, text=True)
    assert r.returncode == 1, "可回补漏网应中止"
    assert "可回补" in r.stdout or "可回补" in r.stderr


# ---------------------------------------------------------------------------
# 质检豁免: 已标记 unrecoverable 的行重写时不再报 suspicious
# ---------------------------------------------------------------------------

def test_exempt_pre_close_skips_rule():
    """check_kline_batch 对 exempt 集内的行跳过 pre_close<=0 规则"""
    records = [_rec("300001", "2026-05-18", pre_close=0)]
    # 无豁免 → 报 suspicious
    flags = check_kline_batch(records)
    assert "300001" in flags
    assert any(f["rule"] == "pre_close_nonpositive" for f in flags["300001"])
    # 有豁免 → 不报 pre_close 规则
    flags = check_kline_batch(records, exempt_pre_close={("300001", "2026-05-18")})
    assert not any(
        f["rule"] == "pre_close_nonpositive"
        for f in flags.get("300001", []))


def test_exempt_does_not_suppress_other_rules():
    """豁免只针对 pre_close 规则, 其余规则 (如 close 越界) 照常"""
    r = _rec("300001", "2026-05-18", pre_close=0, close=5.0)
    r["high"] = 4.0  # close > high → close_out_of_range
    flags = check_kline_batch([r], exempt_pre_close={("300001", "2026-05-18")})
    rules = [f["rule"] for f in flags.get("300001", [])]
    assert "pre_close_nonpositive" not in rules
    assert "close_out_of_range" in rules


def test_gate_process_loads_exempt_from_flag_table(tmp_path):
    """KlineQualityGate.process 从 data_quality_flags 读豁免集, 已标记行不重报"""
    db = _make_db(tmp_path)
    conn = db.get_connection()
    # 预置一条 unrecoverable 标记
    conn.executescript("""
        CREATE TABLE data_quality_flags (
            code TEXT NOT NULL, date TEXT NOT NULL,
            table_name TEXT NOT NULL DEFAULT 'daily_klines',
            field TEXT NOT NULL, flag TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '', rule TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
            PRIMARY KEY (code, date, table_name, field, rule));
    """)
    conn.execute(
        "INSERT INTO data_quality_flags (code, date, field, flag, rule) "
        "VALUES ('300001', '2026-05-18', 'pre_close', 'unrecoverable', "
        "'pre_close_unrecoverable')")
    conn.commit()

    gate = KlineQualityGate(db)
    records = [_rec("300001", "2026-05-18", pre_close=0)]
    gate.process(records, source="test")

    # 不应新增 pre_close_nonpositive 标记
    rows = conn.execute(
        "SELECT rule FROM data_quality_flags WHERE code='300001'").fetchall()
    rules = [r["rule"] for r in rows]
    assert "pre_close_nonpositive" not in rules
    assert "pre_close_unrecoverable" in rules


def test_gate_process_flags_unmarked_bad_pre_close(tmp_path):
    """未标记的 pre_close<=0 行仍照常报 suspicious (豁免不误伤新坏数据)"""
    db = _make_db(tmp_path)
    gate = KlineQualityGate(db)
    records = [_rec("999999", "2026-08-22", pre_close=0)]
    gate.process(records, source="test")
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT rule FROM data_quality_flags WHERE code='999999'").fetchall()
    assert any(r["rule"] == "pre_close_nonpositive" for r in rows)

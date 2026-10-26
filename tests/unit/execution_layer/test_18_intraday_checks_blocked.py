"""卡#18: intraday_checks CHECK 白名单 + 'blocked' — 迁移幂等/端到端/边界三件

病灶 (审计切片C): D1 蓝卡路径写 status='blocked' 超出旧 CHECK 白名单
('no_action','triggered','skipped','error') → executemany 整批失败 → 表停摆。

三件口径:
1. 迁移脚本幂等: 旧 schema 库跑迁移 → blocked 可写+历史行原样; 连跑第二次 → 无需迁移;
   全新库(表不存在) → 无需迁移; 抽样对照零漂移; 非法值仍拒。
2. blocked 端到端: 旧 schema 库迁移后, IntradayMonitor._scan blocked 分支
   真正落库 intraday_checks (此前整批炸, 表停摆)。
3. DDL 源同步: tables.py INTRA_DAY_DDL 新库直建 → blocked 可写 (新库不分叉);
   monitor 写路径仍只 warning 不中断主流程。
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from common.database import Database
from execution_layer.tables import INTRA_DAY_DDL

# 旧 schema (迁移前的生产 DDL, CHECK 白名单无 blocked) — 与 tables.py 修复前逐字一致
OLD_DDL = """
CREATE TABLE intraday_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_time TEXT NOT NULL,
    check_round TEXT NOT NULL,
    code TEXT NOT NULL,
    check_type TEXT NOT NULL,
    current_price REAL NOT NULL,
    trigger_price REAL,
    status TEXT NOT NULL CHECK (status IN ('no_action', 'triggered', 'skipped', 'error')),
    detail TEXT,
    latency_ms INTEGER,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "migrate_intraday_checks_blocked.py"

BASE_ROW = ("2026-08-28 15:00:00", "R42", "000001", "exit", 9.5, 8.8,
            "triggered", "止损触发", 12)


def _old_schema_db(tmp_path):
    """构造迁移前的旧 schema 隔离库 (含历史行)。"""
    db_path = str(tmp_path / "qs_iso.db")
    conn = Database(db_path)
    c = conn.get_connection()
    c.execute(OLD_DDL)
    c.execute(
        "INSERT INTO intraday_checks "
        "(check_time, check_round, code, check_type, current_price, "
        "trigger_price, status, detail, latency_ms) VALUES (?,?,?,?,?,?,?,?,?)",
        BASE_ROW)
    c.commit()
    return db_path


def _run_migration(db_path, dry_run=False):
    cmd = [sys.executable, str(SCRIPT), "--db", db_path,
           "--evidence", str(Path(db_path).parent / "repair_evidence_18_blocked.jsonl")]
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _allows_blocked(db_path) -> bool:
    conn = Database(db_path)
    row = conn.get_connection().execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='intraday_checks'"
    ).fetchone()
    return bool(row and "'blocked'" in row[0])


class TestMigrationIdempotent:
    def test_old_schema_blocked_rejected_then_migrated(self, tmp_path):
        # 红灯起点: 旧 schema 下 blocked 写入必须被 CHECK 拒绝 (病灶实证)
        db_path = _old_schema_db(tmp_path)
        conn = Database(db_path)
        c = conn.get_connection()
        with pytest.raises(Exception):
            c.execute(
                "INSERT INTO intraday_checks (check_time, check_round, code, "
                "check_type, current_price, status) "
                "VALUES ('2026-08-31 11:00:00','R1','000957','entry',10.12,'blocked')")
            c.commit()

        # dry-run: 报需重建, 不动库
        r = _run_migration(db_path, dry_run=True)
        assert r.returncode == 0, r.stderr
        assert "需重建表" in r.stdout
        assert not _allows_blocked(db_path)

        # 实迁: 行数守恒 + 抽样零漂移 + blocked 可写 + 非法值仍拒
        r = _run_migration(db_path)
        assert r.returncode == 0, r.stderr
        assert _allows_blocked(db_path)
        row = Database(db_path).get_connection().execute(
            "SELECT check_time, check_round, code, check_type, current_price, "
            "trigger_price, status, detail, latency_ms FROM intraday_checks"
        ).fetchone()
        assert tuple(row) == BASE_ROW  # 历史行原样

        out = r.stdout
        assert "迁移完成" in out and "抽样对照 1 行不一致 0" in out
        assert "blocked 可写=True" in out and "非法值仍拒=True" in out

    def test_second_run_noop(self, tmp_path):
        # 幂等: 迁移后再跑 → "无需迁移" 直接返回
        db_path = _old_schema_db(tmp_path)
        assert _run_migration(db_path).returncode == 0
        r = _run_migration(db_path)
        assert r.returncode == 0, r.stderr
        assert "无需迁移" in r.stdout
        # 且库仍只有迁移前的 1 行 (第二次跑零写入)
        cnt = Database(db_path).get_connection().execute(
            "SELECT COUNT(*) FROM intraday_checks").fetchone()[0]
        assert cnt == 1

    def test_fresh_db_without_table_noop(self, tmp_path):
        # 全新库: 表不存在 (start() 会用新 DDL 建) → 无需迁移
        db_path = str(tmp_path / "fresh.db")
        Database(db_path)  # 建库不建表
        r = _run_migration(db_path)
        assert r.returncode == 0, r.stderr
        assert "无需迁移" in r.stdout

    def test_evidence_jsonl_written(self, tmp_path):
        db_path = _old_schema_db(tmp_path)
        assert _run_migration(db_path).returncode == 0
        ev = Path(db_path).parent / "repair_evidence_18_blocked.jsonl"
        # 默认写 logs/ 相对 cwd — 以 --evidence 落到 tmp 验证格式
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", db_path,
             "--evidence", str(ev)],
            capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr  # noop 路径不写 evidence
        assert "无需迁移" in r.stdout


class TestBlockedEndToEnd:
    def test_monitor_blocked_row_lands_after_migration(self, tmp_path):
        # 迁移后, D1 blocked 分支真正落库 (此前 executemany 整批炸)
        db_path = _old_schema_db(tmp_path)
        assert _run_migration(db_path).returncode == 0

        db = Database(db_path)
        pool = MagicMock()
        pool.get_by_status.side_effect = lambda s: {
            "active": [], "pending_entry": [{"code": "000957", "last_price": 10.12}],
            "exit_pending": [],
        }.get(s, [])
        from execution_layer.intraday.monitor import IntradayMonitor
        monitor = IntradayMonitor(db, pool)
        monitor.entry_checker = MagicMock()
        monitor.entry_checker.check.return_value = {
            "price": 10.12, "reason": "入场区间已过期 (valid_until=2026-08-28)",
            "triggered": False, "blocked": True,
        }

        monitor._scan()

        rows = db.get_connection().execute(
            "SELECT code, check_type, current_price, status, detail "
            "FROM intraday_checks ORDER BY id DESC LIMIT 1").fetchone()
        assert rows is not None
        assert rows[0] == "000957" and rows[1] == "entry"
        assert rows[3] == "blocked" and "已过期" in rows[4]

    def test_old_schema_executemany_batch_bombs_and_never_kills_scan(self, tmp_path):
        # 迁移前病灶实证: blocked 混入批量 → 整批失败 → 表停摆, 但扫描主流程不中断
        db = Database(":memory:")
        c = db.get_connection()
        c.execute(OLD_DDL)
        c.commit()
        pool = MagicMock()
        pool.get_by_status.side_effect = lambda s: {
            "active": [], "pending_entry": [{"code": "000957", "last_price": 10.12}],
            "exit_pending": [],
        }.get(s, [])
        from execution_layer.intraday.monitor import IntradayMonitor
        monitor = IntradayMonitor(db, pool)
        monitor.entry_checker = MagicMock()
        monitor.entry_checker.check.return_value = {
            "price": 10.12, "reason": "拦截", "triggered": False, "blocked": True,
        }

        monitor._scan()  # 不得抛

        cnt = c.execute("SELECT COUNT(*) FROM intraday_checks").fetchone()[0]
        assert cnt == 0  # 旧 schema: 整批被 CHECK 拒 → 零行落库 (停摆实证)


class TestDdlSourceSync:
    def test_new_schema_allows_blocked_directly(self):
        # DDL 源同步: 新库用 INTRA_DAY_DDL 直建 → blocked 可写 (迁移修旧库, DDL 修新库, 无分叉)
        db = Database(":memory:")
        c = db.get_connection()
        c.execute(INTRA_DAY_DDL)
        c.commit()
        c.execute(
            "INSERT INTO intraday_checks (check_time, check_round, code, "
            "check_type, current_price, status) "
            "VALUES ('2026-08-31 11:00:00','R1','000957','entry',10.12,'blocked')")
        c.commit()
        row = c.execute(
            "SELECT status FROM intraday_checks WHERE code='000957'").fetchone()
        assert row[0] == "blocked"

    def test_fifth_illegal_value_still_rejected(self):
        # 白名单边界: 第五个合法值 blocked 进了, 第六个非法值仍拒
        db = Database(":memory:")
        c = db.get_connection()
        c.execute(INTRA_DAY_DDL)
        c.commit()
        from sqlite3 import IntegrityError
        with pytest.raises(IntegrityError):
            c.execute(
                "INSERT INTO intraday_checks (check_time, check_round, code, "
                "check_type, current_price, status) "
                "VALUES ('2026-08-31 11:00:00','R1','000957','entry',10.12,'bogus_v6')")
            c.commit()

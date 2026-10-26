"""锁窗卡: 写失败语句未回滚遗留写锁 — 复现+修后行为

根因 (2026-08-30 F 取证, round3 探针实证, 统筹立卡):
  任意写语句 (UPDATE/INSERT/DELETE) 撞约束 (如 CHECK) 抛 IntegrityError 时,
  sqlite3 语句级写事务已开启且**不自动回滚**; 若调用方 except 吞异常且不
  rollback/close, 连接挂着写锁被丢弃 — 后续任何连接 (含跨进程) 写同库
  全部 'database is locked', busy_timeout 等不到 (持有者已死)。
  T3 现场重建: 旧 _backfill_run 透传 'shutdown' 撞 CHECK → IntegrityError
  被 _recover_fallback_records except Exception 吞 → _get_db() 的 Database
  连接悬挂带锁 → 同文件下一测试新连接撞锁 (RED 期 t2.log 实证)。
  e63eb97 (CHECK 卡) 已治状态域 (白名单归一不再撞 CHECK), 本卡治锁域:
  写失败即释放锁, 吞异常的调用方不再能留锁伤人。

修法 (连接源单点兜底, 不动 aa25e99 单连接源不变量):
  get_connection 以 factory= 下发 _LockSafeConnection (sqlite3.Connection
  子类): 写语句失败时立即 rollback() 释放语句级写事务再原样上抛 — 覆盖
  Database.execute/executemany **与全部裸用 get_connection 的调用方**
  (pipeline_logger/run_guard 及未来新增), 无需逐点包 try。
  只回滚「失败语句自己开启的事务」(失败前无事务+失败后有事务) — SELECT
  失败不会开启写事务故不触发, 调用方多语句事务中途失败时待写内容不被动
  丢弃。get_connection 缓存/total_changes 探针/单连接源全部不动。
  与既有惯例互补: retry_on_locked/busy_timeout 治「锁被活人持有会释放」;
  本修法治「锁被死连接持有永不释放」— 语义不同层, 不冲突。
"""
import sqlite3

import pytest

from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL

_DDL_BAD_CHECK = """
CREATE TABLE pipeline_runs_probe (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('running', 'success'))
)
"""


class _SecondConn:
    """独立第二连接 (模拟另一 Database 实例/进程), busy_timeout 压到 0.5s。"""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=0.5)

    def try_insert(self) -> bool:
        """写入并提交; 撞锁返回 False。"""
        try:
            self.conn.execute(
                "INSERT INTO pipeline_runs_probe (run_id, status) VALUES ('r2', 'running')")
            self.conn.commit()
            return True
        except sqlite3.OperationalError:
            return False


def test_write_failure_releases_lock(tmp_path):
    """复现+修: Database.execute 写语句撞 CHECK 后, 第二连接必须能立刻写入
    (现状: 失败事务不回滚, 写锁挂在死连接上, 第二连接 busy_timeout 等不到)。"""
    db_path = str(tmp_path / "lock.db")
    db = Database(db_path)
    conn = db.get_connection()
    conn.executescript(_DDL_BAD_CHECK)
    # 先放一行合法行, 让 UPDATE 真命中触发 CHECK (0 行命中不触发约束)
    conn.execute("INSERT INTO pipeline_runs_probe (run_id, status) VALUES ('r1', 'running')")
    conn.commit()

    # 写失败: UPDATE 命中 r1 改非法值撞 CHECK (调用方吞异常, rollback 由 Database 层负责)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE pipeline_runs_probe SET status='shutdown' WHERE run_id='r1'")
    # 模拟调用方 except Exception: pass — 不 rollback 不 close, 直接跑路

    second = _SecondConn(db_path)
    assert second.try_insert(), (
        "写失败的锁未释放: 第二连接撞 'database is locked' — "
        "失败语句的事务必须当场回滚, 不得让吞异常的调用方留锁伤人")
    second.conn.close()
    db.close()


def test_read_after_failed_write_still_works(tmp_path):
    """写失败回滚后, 原连接自身仍可读 (rollback 只清写事务, 不杀连接)。"""
    db_path = str(tmp_path / "lock2.db")
    db = Database(db_path)
    conn = db.get_connection()
    conn.executescript(_DDL_BAD_CHECK)
    conn.execute("INSERT INTO pipeline_runs_probe (run_id, status) VALUES ('r1', 'running')")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE pipeline_runs_probe SET status='shutdown' WHERE run_id='r1'")
    # 原连接继续读 — 单连接源不受影响
    row = conn.execute("SELECT COUNT(*) FROM pipeline_runs_probe").fetchone()
    assert row[0] == 1
    # 原连接继续写合法语句
    db.execute("INSERT INTO pipeline_runs_probe (run_id, status) VALUES ('r3', 'running')")
    db.commit()
    db.close()


def test_commit_failure_also_releases_lock(tmp_path):
    """裸 get_connection 的失败写事务同样不得留锁 — pipeline_logger/run_guard
    均裸用 get_connection, Database.execute 包装不够, 须在连接源处兜底。"""
    db_path = str(tmp_path / "lock3.db")
    db = Database(db_path)
    conn = db.get_connection()
    conn.executescript(_DDL_BAD_CHECK)
    conn.execute("INSERT INTO pipeline_runs_probe (run_id, status) VALUES ('r1', 'running')")
    conn.commit()

    # 直接在连接上留一个失败写事务 (绕过 db.execute, 模拟裸用 get_connection
    # 的调用方 — run_guard/pipeline_logger 就是裸用):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE pipeline_runs_probe SET status='shutdown' WHERE run_id='r1'")
    # 该失败事务未经 db.execute 包装 — 第二连接应仍可写:
    # 修法若只包 db.execute, 裸路径仍会留锁。此测试锁住「裸路径也要治」。
    second = _SecondConn(db_path)
    assert second.try_insert(), (
        "裸 get_connection 的失败写事务未释放锁 — 修法必须覆盖裸调用方路径 "
        "(pipeline_logger/run_guard 均裸用 get_connection)")
    second.conn.close()
    db.close()

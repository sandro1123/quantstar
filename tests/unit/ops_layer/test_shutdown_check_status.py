"""CHECK 卡: shutdown 路径写 pipeline_runs 被 CHECK 拒 — 复现+修后行为

病灶 (2026-08-29 F 取证):
  OpsManager.shutdown() (src/ops_layer/__init__.py:208) 对所有 status='running'
  的记录写 end_run(run_id, "shutdown", "system shutdown") — 'shutdown' 不在
  pipeline_runs.status CHECK 白名单 ('running','success','partial','failed','skipped')
  → sqlite3.IntegrityError: CHECK constraint failed。e2e_verification.py:204
  finally 分支为唯一脚本触发面 (cron 与 e2e 重叠时必撞)。

裁定 (统筹 2026-08-29): a 变体 — 不发明半状态, 写合法状态 + 原因进 error_msg:
  end_run(run_id, "failed", "system shutdown...")
  run_guard._backfill_run 回灌透传 rec.get("status") 同样加白名单校验,
  非法 status 归一 failed, error_msg 记原值 (不静默吞)。
原则: 宁可NULL不可假值 + 状态机诚实, 五态机是全下游共同语言。
"""
import sqlite3

import pytest

from common.database import Database
from ops_layer.core.tables import OPS_TABLES_DDL
from ops_layer.pipeline_logger import PipelineLogger


def _mk_db_with_running_run() -> tuple[Database, str]:
    """建内存库 + 一条 running 记录, 返回 (db, run_id)。"""
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    pl = PipelineLogger(db)
    run_id = pl.start_run("kline")
    return db, run_id


class TestShutdownPathLegality:
    """OpsManager.shutdown: 'running' 收尾必须写合法 status, 不写 'shutdown'"""

    def test_shutdown_writes_failed_with_reason_not_shutdown(self):
        """复现+修: shutdown 收尾写 failed+error_msg='system shutdown',
        不是 status='shutdown' (后者撞 CHECK 白名单)。"""
        from ops_layer import OpsLayer

        db, _ = _mk_db_with_running_run()
        mgr = OpsLayer(db=db)
        mgr.shutdown(timeout=1)
        conn = db.get_connection()
        row = conn.execute(
            "SELECT status, error_msg FROM pipeline_runs "
            "ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
        # 修后: 合法五态之一 + 原因进 error_msg
        assert row["status"] in ("running", "success", "partial", "failed", "skipped")
        assert "system shutdown" in (row["error_msg"] or "")
        db.close()

    def test_shutdown_no_row_left_running(self):
        """shutdown 后不得残留 running (半状态=下次僵尸收割/卡启动)。"""
        from ops_layer import OpsLayer

        db, _ = _mk_db_with_running_run()
        mgr = OpsLayer(db=db)
        mgr.shutdown(timeout=1)
        conn = db.get_connection()
        n = conn.execute(
            "SELECT COUNT(*) FROM pipeline_runs WHERE status='running'").fetchone()[0]
        assert n == 0, "shutdown 后仍有 running 残留 — 半状态未收官"
        db.close()


class TestBackfillRunValidation:
    """run_guard._backfill_run: fallback 回灌 status 白名单校验"""

    def _backfill(self, db, status, monkeypatch):
        # _backfill_run 内部 _get_db() 自建 Database() (默认指向 QS_TEST_DB/生产库),
        # 与本测试的内存库不是同一个 — 必须 patch 成测试库, 否则写错库。
        import ops_layer.run_guard as rg
        monkeypatch.setattr(rg, "_get_db", lambda: db)
        rg._backfill_run({
            "kind": "pipeline_run", "name": "kline",
            "status": status, "error": "orig-error",
            "version": "", "config_hash": "",
        })

    def test_backfill_invalid_status_normalized_to_failed(self, monkeypatch):
        """非法 status (如 'shutdown') 回灌归一 failed, error_msg 记原值 —
        不静默吞 (现状: 撞 CHECK 被 except Exception 吞掉不回灌),
        不透传撞 CHECK。"""
        db, _ = _mk_db_with_running_run()
        # 修后: 非法 status 归一 failed 落库, 记录原值, 不抛不吞
        self._backfill(db, "shutdown", monkeypatch)
        conn = db.get_connection()
        row = conn.execute(
            "SELECT status, error_msg FROM pipeline_runs "
            "WHERE pipeline_name='kline' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "failed"
        assert "shutdown" in (row["error_msg"] or ""), "原值必须留在 error_msg, 不静默吞"
        db.close()

    def test_backfill_valid_status_passthrough(self, monkeypatch):
        """合法 status 照常透传 (白名单不拦正常回灌)。"""
        db, _ = _mk_db_with_running_run()
        self._backfill(db, "success", monkeypatch)
        conn = db.get_connection()
        row = conn.execute(
            "SELECT status FROM pipeline_runs "
            "WHERE pipeline_name='kline' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "success"
        db.close()

    def test_backfill_unexpected_status_also_normalized(self, monkeypatch):
        """任意白名单外值 (不止 'shutdown') 一律归一 failed — 防未来新增
        非法值再撞 CHECK (如 'interrupted', 统筹③已点名)。"""
        db, _ = _mk_db_with_running_run()
        self._backfill(db, "interrupted", monkeypatch)
        conn = db.get_connection()
        row = conn.execute(
            "SELECT status, error_msg FROM pipeline_runs "
            "WHERE pipeline_name='kline' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "failed"
        assert "interrupted" in (row["error_msg"] or "")
        db.close()

"""ops_layer/pipeline_logger.py — 管道运行日志"""

from dataclasses import fields
from datetime import datetime, timedelta

from common.database import Database
from ops_layer.budget_formula import compute_ceiling  # 判例22 单一定义点
from common.logging import get_logger
from common.utils import bj_date, bj_now
from ops_layer.core.models import PipelineRun

logger = get_logger("ops.pipeline_logger")

# OPS-01: running 记录超过 timeout + 宽限仍未收官即判僵尸 (进程已死/超时 ghost)。
_ZOMBIE_GRACE_S = 300
_ZOMBIE_ERROR_MSG = "zombie reaped: running 记录超过 timeout+宽限仍未收官, 判定进程已死"


class PipelineLogger:
    def __init__(self, db: Database):
        self.db = db

    def start_run(self, pipeline_name: str) -> str:
        from uuid import uuid4

        from ops_layer.version_tracker import (
            _ensure_pipeline_runs_columns,
            compute_effective_config_hash,
        )

        now = bj_now()
        # Retries and concurrent triggers can start in the same second; preserve every attempt.
        run_id = f"{pipeline_name}_{bj_date()}_{now.split(' ')[1].replace(':', '')}_{uuid4().hex[:8]}"
        # 卡V: 入口即打生效配置指纹 + git HEAD — running 态也可溯源 (不必等
        # end_run); 失效场景降级 NULL 不阻断记录 (fail-safe, 管道记录优先)。
        # 双层 fail-safe: compute_effective_config_hash 自身降级空串, 这里再兜
        # 一层 — monkeypatch/极端场景下函数本体炸也不能挡住 run 记录落库。
        try:
            effective_hash = compute_effective_config_hash() or None
        except Exception:
            effective_hash = None
        try:
            from ops_layer.version_tracker import git_head
            head_version = git_head()[0] or None
        except Exception:
            head_version = None
        conn = self.db.get_connection()
        # DL-43: 管道内幂等补列 (老生产表无新列时 ALTER; 幂等吞 duplicate)
        _ensure_pipeline_runs_columns(conn)
        conn.execute("""
            INSERT INTO pipeline_runs
                (run_id, pipeline_name, date, start_time, status,
                 version, config_hash_effective)
            VALUES (?, ?, ?, ?, 'running', ?, ?)
        """, (run_id, pipeline_name, bj_date(), now, head_version, effective_hash))
        conn.commit()
        return run_id

    def end_run(self, run_id: str, status: str, error: str = "", version: str = "", config_hash: str = ""):
        conn = self.db.get_connection()
        now = bj_now()
        row = conn.execute("SELECT start_time FROM pipeline_runs WHERE run_id=?", (run_id,)).fetchone()
        duration_s = 0.0
        if row:
            try:
                start = datetime.strptime(row["start_time"], "%Y-%m-%d %H:%M:%S")
                end = datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                duration_s = (end - start).total_seconds()
            except (ValueError, TypeError):
                pass
        conn.execute("""
            UPDATE pipeline_runs SET end_time=?, status=?, error_msg=?, duration_s=?,
            version=?, config_hash=? WHERE run_id=?
        """, (now, status, error, duration_s, version, config_hash, run_id))
        conn.commit()

    def get_recent_runs(self, days: int = 7) -> list[PipelineRun]:
        conn = self.db.get_connection()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM pipeline_runs WHERE date >= ? ORDER BY start_time DESC", (cutoff,)
        ).fetchall()
        field_names = {f.name for f in fields(PipelineRun)}
        return [PipelineRun(**{k: dict(r)[k] for k in field_names if k in dict(r)}) for r in rows]

    def get_active_runs(self) -> list[dict]:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT run_id, pipeline_name FROM pipeline_runs WHERE status='running'"
        ).fetchall()
        return [dict(r) for r in rows]

    def reap_expired_runs(self, pipeline_name: str | None = None,
                          timeout_lookup: dict[str, int] | None = None,
                          default_timeout_s: int = 300,
                          grace_s: int = _ZOMBIE_GRACE_S,
                          full_cfg: dict[str, dict] | None = None
                          ) -> list[dict]:
        """OPS-01: 收割过期 running 记录为 failed(zombie reaped)。

        判定: status='running' 且 start_time 早于 now - (ceiling + grace_s)。
        ceiling = DL-65 统一公式 (budget_formula 单一定义点, 判例 22):
        timeout×(retries+1)+delay×retries, 带依赖/wait_timeout 取大。
        full_cfg 给 per-pipeline 完整配置 (含 max_retries/retry_delay_s/
        wait_timeout_s); 缺席时回落 timeout_lookup 裸 timeout (兼容旧
        调用方), 但生产调用面 (ops_layer) 必传 full_cfg — 裸面会误杀
        活行 (09-11 ddp 病历: 3000s < watchdog 5550s 被先杀)。
        DL-42: 收割时补填 version/config_hash (铁律 #12 溯源不断)。
        返回被收割记录列表 [{run_id, pipeline_name, start_time}]。
        """
        # DL-42: 收割时计算溯源字段, 与 orchestrator/run_guard 同源
        version, config_hash = "", ""
        try:
            from ops_layer.version_tracker import compute_config_hash, git_head
            version, _branch = git_head()
            config_hash = compute_config_hash()
        except Exception:
            pass

        conn = self.db.get_connection()
        now = datetime.now()
        sql = "SELECT run_id, pipeline_name, start_time FROM pipeline_runs WHERE status='running'"
        params: list = []
        if pipeline_name:
            sql += " AND pipeline_name=?"
            params.append(pipeline_name)
        reaped = []
        for row in conn.execute(sql, params).fetchall():
            try:
                start = datetime.strptime(row["start_time"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            pname = row["pipeline_name"]
            # 判例 22 同源修: 优先 full_cfg (完整 DL-65 ceiling 含重试/等待窗),
            # 缺席回落裸 timeout_lookup (兼容旧调用方 — 语义不宽于旧面)。
            if full_cfg and pname in full_cfg:
                ceiling = compute_ceiling(full_cfg[pname])
            else:
                ceiling = (timeout_lookup or {}).get(pname, default_timeout_s)
            if (now - start).total_seconds() > ceiling + grace_s:
                conn.execute(
                    "UPDATE pipeline_runs SET status='failed', error_msg=?, end_time=?, "
                    "version=?, config_hash=? WHERE run_id=?",
                    (_ZOMBIE_ERROR_MSG, bj_now(), version, config_hash, row["run_id"]),
                )
                reaped.append(dict(row))
                logger.warning(
                    f"zombie reaped: {row['pipeline_name']} run_id={row['run_id']} "
                    f"start={row['start_time']} (超过 ceiling {ceiling}s + 宽限 {grace_s}s)"
                )
        if reaped:
            conn.commit()
        return reaped

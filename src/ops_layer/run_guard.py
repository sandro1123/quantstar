"""ops_layer/run_guard.py — F6.1 直连脚本可观测哨兵

cron 直接触发的脚本 (intraday_scan / daily_update / e2e_verification /
api_health_check) 不走 orchestrator, 崩溃时既不留 pipeline_runs 记录也不告警。
run_guarded() 统一包装入口函数:

  - 成功 (返回 0/None)  → pipeline_runs status=success
  - 异常               → pipeline_runs status=failed + traceback + critical 告警, 重新抛出
  - 非零退出码         → pipeline_runs status=failed (+ warning 告警, 可关)

可观测链路自身 fail-safe: 记录/告警失败只 warning, 不掩盖脚本原始结果。

P0-5 (铁律 #12 可观测不可静默): OPS-04 retry_on_locked 重试耗尽后原实现
return None 静默丢弃 pipeline_runs 记录 (08-21 16:00 factor_update 库内零痕迹)。
现改为降级到第二通道:
  - 写 logs/observability_fallback.jsonl (结构化, 含本该写入的完整记录)
  - stderr 打印显著告警
  - 返回 DEGRADED 哨兵区分"成功"与"降级", 调用方可感知
并补恢复机制: 下次 run_guarded 启动时把 fallback 里未回灌的记录补充写
pipeline_runs / 重发告警, 成功后标记 recovered。
"""
import json
import os
import sqlite3
import sys
import time
import traceback

from common.logging import get_logger

logger = get_logger("ops.run_guard")

_ERROR_MSG_LIMIT = 2000


class _Degraded:
    """哨兵: 重试耗尽已降级到 fallback 通道 (区别于 None=成功)。"""

    def __repr__(self):
        return "<DEGRADED>"

    def __bool__(self):
        return False


DEGRADED = _Degraded()


def _fallback_path() -> str:
    """fallback jsonl 路径 (QS_OBSERVABILITY_FALLBACK 可覆盖, 供测试)。"""
    override = os.environ.get("QS_OBSERVABILITY_FALLBACK")
    if override:
        return override
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "logs", "observability_fallback.jsonl")


def _write_fallback(record: dict) -> None:
    """铁律 #12: 观测链路自身失败 → 降级第二通道 (jsonl + stderr), 绝不静默。"""
    record = dict(record)
    record.setdefault("fallback_ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    record.setdefault("recovered", False)
    path = _fallback_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        # 最后通道也失败 — 至少 stderr 显著告警, 不能无声
        print(f"[run_guard][FALLBACK-FAILED] 无法写 fallback jsonl: {e}; record={record}",
              file=sys.stderr, flush=True)
        return
    print(f"[run_guard][OBSERVABILITY-DEGRADED] {record.get('kind')}:{record.get('name')} "
          f"落库失败(locked), 已降级到 {path}", file=sys.stderr, flush=True)


def retry_on_locked(fn, *, attempts: int = 3, delay_s: float = 2.0, label: str = "",
                    fallback_record: dict | None = None):
    """OPS-04: database is locked 时重试落库 (默认 3 次 × 2s)。

    08-20 事故: factor_update/health_check 崩于锁, 崩溃落库路径二次撞锁双重失败。
    重试给锁持有者释放时间。

    P0-5 (铁律 #12): attempts 耗尽后不再静默 return None — 若提供 fallback_record,
    降级写 logs/observability_fallback.jsonl + stderr 告警, 并返回 DEGRADED 哨兵;
    未提供 fallback_record 时保持旧行为返回 None (向后兼容)。非 locked 的
    OperationalError 原样抛出, 绝不掩盖脚本原始异常。
    """
    name = label or getattr(fn, "__name__", "fn")
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            last_err = e
            if attempt < attempts:
                logger.warning(f"retry_on_locked({name}): database is locked, {delay_s}s 后重试 ({attempt}/{attempts - 1})")
                time.sleep(delay_s)
    logger.warning(f"retry_on_locked({name}): {attempts} 次尝试后仍 locked, 降级 fallback: {last_err}")
    if fallback_record is not None:
        fallback_record = dict(fallback_record)
        fallback_record.setdefault("reason", f"database locked after {attempts} attempts: {last_err}")
        _write_fallback(fallback_record)
        return DEGRADED
    return None


def _get_db():
    from common.database import Database

    return Database()


def _ensure_ops_tables(db) -> None:
    from execution_layer.migrations import ensure_schema
    from ops_layer.core.tables import OPS_TABLES_DDL

    conn = db.get_connection()
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    # 独立脚本不经过层 start()；CREATE IF NOT EXISTS 不会给旧 alerts 补列。
    ensure_schema(db)


def _recover_fallback_records() -> None:
    """P0-5 恢复机制: run_guarded 启动时把 fallback 里未回灌的记录补写。

    pipeline_run → 补写 pipeline_runs; alert → 重发告警。成功后标记 recovered
    并重写文件。DB 仍 locked 则保留待下次。fail-safe, 不阻断主流程。
    """
    path = _fallback_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except Exception as e:
        logger.warning(f"run_guard 读取 fallback 失败: {e}")
        return

    remaining = []
    n_recovered = 0
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            remaining.append(ln)
            continue
        if rec.get("recovered"):
            continue
        try:
            if rec.get("kind") == "pipeline_run":
                _backfill_run(rec)
            elif rec.get("kind") == "alert":
                _resend_alert(rec)
            else:
                remaining.append(ln)
                continue
            n_recovered += 1
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                remaining.append(ln)  # 仍 locked, 留待下次
            else:
                logger.warning(f"run_guard 回灌 fallback 记录失败: {e}")
        except Exception as e:
            logger.warning(f"run_guard 回灌 fallback 记录失败: {e}")

    if n_recovered:
        logger.info(f"run_guard 从 fallback 回灌 {n_recovered} 条记录")
    try:
        with open(path, "w", encoding="utf-8") as f:
            for ln in remaining:
                f.write(ln + "\n")
    except Exception as e:
        logger.warning(f"run_guard 重写 fallback 文件失败: {e}")


def _backfill_run(rec: dict) -> None:
    from ops_layer.pipeline_logger import PipelineLogger

    db = _get_db()
    _ensure_ops_tables(db)
    pl = PipelineLogger(db)
    run_id = pl.start_run(rec.get("name", "unknown"))
    # CHECK 卡 (2026-08-29): status 白名单校验 — pipeline_runs.status CHECK 只容
    # 五态, fallback 记录里任何白名单外值 (shutdown/interrupted/...) 透传必撞
    # IntegrityError 且被上层 except 吞掉 = 记录丢失。归一 failed + 原值进
    # error_msg: 状态机诚实 (run 确实异常收尾), 不发明半状态, 不静默吞。
    _VALID_STATUS = ("running", "success", "partial", "failed", "skipped")
    raw_status = rec.get("status", "failed") or "failed"
    if raw_status in _VALID_STATUS:
        status, error = raw_status, (rec.get("error") or "")[:_ERROR_MSG_LIMIT]
    else:
        status = "failed"
        orig = (rec.get("error") or "")[:_ERROR_MSG_LIMIT]
        error = (f"invalid status normalized: original_status={raw_status!r}; {orig}"
                 )[:_ERROR_MSG_LIMIT]
    pl.end_run(run_id, status, error,
               rec.get("version", ""), rec.get("config_hash", ""))


def _resend_alert(rec: dict) -> None:
    from ops_layer.alert_manager import AlertManager
    from ops_layer.core.models import Alert

    db = _get_db()
    _ensure_ops_tables(db)
    AlertManager(db).send(Alert(
        severity=rec.get("severity", "warning"), layer="ops",
        title=f"直连脚本异常(补发): {rec.get('name', 'unknown')}",
        detail=(rec.get("detail") or "")[:_ERROR_MSG_LIMIT],
        action="检查对应脚本日志并手动干预",
    ))


def _record_run(name: str, status: str, error: str = ""):
    """写一条 pipeline_runs 记录 (fail-safe; OPS-04: locked 时重试; P0-5: 耗尽降级 fallback)。

    OPS-06: 记录带 version/config_hash (复用 version_tracker 模块级函数,
    与 DL-13 orchestrator 溯源同源), 失败降级为空串不阻断记录。
    返回 DEGRADED 表示已降级到 fallback 通道 (铁律 #12), 否则 None。
    """
    version, config_hash = "", ""
    try:
        from ops_layer.pipeline_logger import PipelineLogger
        from ops_layer.version_tracker import compute_config_hash, git_head

        try:
            version, _branch = git_head()
            config_hash = compute_config_hash()
        except Exception:
            version, config_hash = "", ""

        def _write():
            db = _get_db()
            _ensure_ops_tables(db)
            pl = PipelineLogger(db)
            run_id = pl.start_run(name)
            pl.end_run(run_id, status, error[:_ERROR_MSG_LIMIT], version, config_hash)

        return retry_on_locked(_write, label=f"record_run:{name}", fallback_record={
            "kind": "pipeline_run", "name": name, "status": status,
            "error": error[:_ERROR_MSG_LIMIT], "version": version, "config_hash": config_hash,
        })
    except Exception as e:
        logger.warning(f"run_guard 记录 pipeline_runs 失败 ({name}): {e}")
        return None


def _send_alert(name: str, severity: str, detail: str):
    """飞书告警 (fail-safe, AlertManager 自带 30 分钟去重; OPS-04: locked 重试; P0-5: 耗尽降级 fallback)。"""
    try:
        from ops_layer.alert_manager import AlertManager
        from ops_layer.core.models import Alert

        def _write():
            db = _get_db()
            _ensure_ops_tables(db)
            AlertManager(db).send(Alert(
                severity=severity, layer="ops",
                title=f"直连脚本异常: {name}",
                detail=detail[:_ERROR_MSG_LIMIT],
                action="检查对应脚本日志并手动干预",
            ))

        return retry_on_locked(_write, label=f"send_alert:{name}", fallback_record={
            "kind": "alert", "name": name, "severity": severity,
            "detail": detail[:_ERROR_MSG_LIMIT],
        })
    except Exception as e:
        logger.warning(f"run_guard 告警发送失败 ({name}): {e}")
        return None


def run_guarded(name: str, fn, *, alert_on_exit_code: bool = True):
    """包装直连脚本入口函数, 原样返回 fn 的返回值。

    - fn 抛异常 → 记 failed + critical 告警, 重新抛出 (保留原退出语义)
    - fn 返回非零 int → 记 failed + warning 告警 (alert_on_exit_code=False 可关,
      用于自带告警的脚本如 e2e_verification / api_health_check)
    - fn 返回 0/None → 记 success

    P0-5: 启动时先回灌 fallback 通道里上次降级未落库的记录 (铁律 #12 恢复机制)。
    """
    try:
        _recover_fallback_records()
    except Exception as e:
        logger.warning(f"run_guard fallback 回灌异常 (不阻断): {e}")

    try:
        result = fn()
    except Exception:
        tb = traceback.format_exc()
        logger.exception(f"run_guard 捕获直连脚本异常: {name}")
        _record_run(name, "failed", tb)
        _send_alert(name, "critical", f"脚本抛出异常:\n{tb}")
        raise
    exit_code = result if isinstance(result, int) else 0
    if exit_code != 0:
        _record_run(name, "failed", f"exit code {exit_code}")
        if alert_on_exit_code:
            _send_alert(name, "warning",
                        f"脚本以非零退出码结束: {exit_code} (详见脚本日志)")
    else:
        _record_run(name, "success")
    return result

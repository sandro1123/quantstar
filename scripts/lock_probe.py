#!/usr/bin/env python3
"""Q4: BEGIN IMMEDIATE 锁健康探针 CLI — scripts/lock_probe.py

进程外独立实证「SQLite 写锁是否可获取」。供 cron_watchdog.sh 在杀进程后
(RC=124 分支) 调用, 验证锁确实随进程死亡释放; 也可独立 cron 周期巡检。

三分支 + 退出码:
  healthy  -> exit 0  (写锁可获取; 顺带 resolve 已结束的 ghost)
  locked   -> exit 3  (写锁被占, critical 告警 — 08-21 ghost 持锁的显式信号)
  degraded -> exit 4  (探针自身异常, 已 ro 降级, 不误报锁问题)
  throttled-> exit 0  (频率限制内跳过, 非故障)

三约束 (铁律 #9 例外已获批): 短 busy_timeout / try-finally rollback / 频率限制,
另保留 ro 降级路径。配置读 config/defaults/ops.yaml ops.lock_probe (D-31 单一事实源)。

用法:
  PYTHONPATH=src .venv/bin/python scripts/lock_probe.py [--force] [--db PATH]
  --force 绕过频率节流 (watchdog 杀后验证用, 确保每次都真测)
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ops_layer.lock_probe import probe  # noqa: E402

_EXIT = {"healthy": 0, "locked": 3, "degraded": 4, "throttled": 0}


def _load_cfg(yaml_path: str) -> dict:
    try:
        import yaml
        with open(yaml_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return ((cfg.get("ops", {}) or {}).get("lock_probe", {}) or {})
    except Exception:
        return {}


def _jsonl_path() -> str:
    override = os.environ.get("QS_LOCK_PROBE_LOG")
    if override:
        return override
    root = Path(__file__).resolve().parent.parent
    return str(root / "logs" / "lock_probe.jsonl")


def _emit(event: dict) -> None:
    path = _jsonl_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:  # 观测通道失败必须出声 (铁律 #12)
        print(f"[lock_probe][EVENT-FAILED] 无法写 jsonl: {e}; event={event}", flush=True)


def _alert_locked(detail: str) -> None:
    """locked 分支发 critical 告警。尽力而为: 库被占时告警写入本身可能失败, 不阻塞。"""
    try:
        from common.database import Database
        from ops_layer.alert_manager import AlertManager
        from ops_layer.core.models import Alert
        am = AlertManager(Database())
        am.send(Alert(
            severity="critical", layer="ops",
            title="SQLite 写锁被占 (lock_probe)",
            detail=detail + " — 疑似 ghost/孤儿进程持锁 (08-21 事故机制), 请查 lock_probe.jsonl 与活动进程",
            action="排查持锁进程: ps aux | grep python; 必要时按 Ghost 真杀预案处置",
        ))
    except Exception as e:
        print(f"[lock_probe][ALERT-FAILED] locked 告警发送失败 (库可能被占): {e}", flush=True)


def _resolve_ghosts() -> None:
    """healthy 分支: 锁已可获取 => 写跨进程 ghost_lock_released 标记 (闭环)。

    GhostRegistry 内存登记随 orchestrator 进程死亡而消失, 跨进程闭环走 jsonl
    标记; 尽力而为, 失败不阻塞探针。
    """
    try:
        from ops_layer.ghost_registry import emit_lock_released
        emit_lock_released("lock_probe BEGIN IMMEDIATE 成功, 写锁可获取")
    except Exception as e:
        print(f"[lock_probe][RESOLVE-FAILED] ghost_lock_released 标记失败 (不影响探针): {e}", flush=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="绕过频率节流")
    ap.add_argument("--db", default=None, help="库路径 (缺省 data/qs.db)")
    args = ap.parse_args(argv[1:])

    root = Path(__file__).resolve().parent.parent
    cfg = _load_cfg(str(root / "config" / "defaults" / "ops.yaml"))
    # 优先级: --db > QS_LOCK_PROBE_DB(测试/隔离) > ops.yaml > 缺省 data/qs.db
    db_path = (args.db or os.environ.get("QS_LOCK_PROBE_DB")
               or cfg.get("db_path") or str(root / "data" / "qs.db"))
    busy_ms = int(cfg.get("busy_timeout_ms", 2000))
    min_interval = float(cfg.get("min_interval_s", 60))

    result = probe(db_path, busy_timeout_ms=busy_ms,
                   min_interval_s=min_interval, force=args.force)
    status = result["status"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _emit({"event": "lock_probe", "ts": ts, "status": status,
           "db": db_path, "detail": result["detail"],
           "elapsed_ms": round(result["elapsed_ms"], 1)})

    if status == "locked":
        _alert_locked(result["detail"])
        print(f"[lock_probe] LOCKED db={db_path}: {result['detail']}", file=sys.stderr)
    elif status == "healthy":
        _resolve_ghosts()
        print(f"[lock_probe] healthy db={db_path} ({result['elapsed_ms']:.0f}ms)", file=sys.stderr)
    elif status == "degraded":
        print(f"[lock_probe] DEGRADED db={db_path}: {result['detail']}", file=sys.stderr)
    else:  # throttled
        print(f"[lock_probe] throttled ({result['detail']})", file=sys.stderr)

    return _EXIT.get(status, 4)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

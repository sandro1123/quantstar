"""G7-A/G3-B 僵尸行清理 (D-285 三判据版) — 只 UPDATE 状态, 永不 DELETE。

与 scripts/reap_zombie_runs.py (OPS-01 单判据: timeout+300s) 的区别:
本脚本三判据全中才判死, 面向「活体故障期」的谨慎清理 —

  C1 无活体进程: /proc 全表扫描, 无任何 cmdline 匹配该管道的启动形态
     (识别 "pipeline run <name>" 与 "scripts.<name>" / "scripts/<name>" 两种直驱形态;
      未来新增启动形态须扩此表 —— 不确定就漏收成 suspect, 不误杀)
  C2 version 空: version 与 config_hash 均为 NULL/'' — end_run 从未写过,
     即进程死于收官之前 (正常结束/超时路径都会写溯源字段, DL-42)
  C3 超时窗×2: now - start_time > timeout_s × 2 (timeout 读 ops.yaml,
     与 orchestrator 同源; ×2 是给 watchdog 重启档留的余量)

三判据缺一 → 归入 suspect 只报告不动手。

用法 (生产执行走写库三步曲, 且须统筹确认时序后才 --apply):
  PYTHONPATH=src .venv/bin/python scripts/clear_zombie_runs.py                 # dry-run
  PYTHONPATH=src .venv/bin/python scripts/clear_zombie_runs.py --apply --backup
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

PROC_DIR = "/proc"
_DEFAULT_TIMEOUT_S = 300
_AUDIT_NOTE = "zombie reaped: clear_zombie_runs 三判据(无进程+version空+超时窗x2), D-285"


def find_live_pids(proc_dir: str, markers: tuple[str, ...]) -> list[int]:
    """扫描 proc_dir 下全部进程 cmdline, 返回命中任一 marker 的 pid 列表。"""
    pids: list[int] = []
    if not os.path.isdir(proc_dir):
        return pids  # 非 Linux (如测试环境) 视为无活体 — 由调用方决定是否可信
    for entry in os.listdir(proc_dir):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc_dir, str(entry), "cmdline"), "rb") as f:
                cmd = f.read().decode("utf-8", "replace").replace("\x00", " ")
        except OSError:
            continue  # 进程恰在退出, 不算命中也不报错
        if any(m in cmd for m in markers):
            pids.append(int(entry))
    return pids


def _launch_markers(name: str) -> tuple[str, ...]:
    """管道名的启动形态标记表。cron 与手工都走这两种; 新形态须在此登记。"""
    return (f"pipeline run {name}", f"scripts.{name}", f"scripts/{name}")


def _missing(v) -> bool:
    return v is None or v == ""


def select_zombies(conn, now: datetime, timeout_lookup: dict[str, int],
                   proc_dir: str = PROC_DIR) -> tuple[list[dict], list[dict]]:
    """三判据筛分 running 记录。返回 (zombies, suspects)。

    zombies/suspects 元素: {run_id, pipeline_name, start_time, age_s, detail}
    """
    zombies: list[dict] = []
    suspects: list[dict] = []
    rows = conn.execute(
        "SELECT run_id, pipeline_name, start_time, version, config_hash "
        "FROM pipeline_runs WHERE status='running'"
    ).fetchall()
    # 同名管道的活体检测一次扫描共享结果 (无法区分同管道多个 run 时从保守: 有活体即全 suspect)
    live_cache: dict[str, list[int]] = {}
    for row in rows:
        rec = dict(row)
        name = rec["pipeline_name"]
        try:
            start = datetime.strptime(rec["start_time"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            suspects.append({**rec, "detail": f"start_time 不可解析: {rec['start_time']!r}"})
            continue
        age_s = (now - start).total_seconds()
        timeout_s = int(timeout_lookup.get(name, _DEFAULT_TIMEOUT_S))
        base = {"run_id": rec["run_id"], "pipeline_name": name,
                "start_time": rec["start_time"], "age_s": age_s}

        c1_ok = False
        if name not in live_cache:
            live_cache[name] = find_live_pids(proc_dir, _launch_markers(name))
        pids = live_cache[name]
        c1_ok = not pids
        c2_ok = _missing(rec["version"]) and _missing(rec["config_hash"])
        c3_ok = age_s > timeout_s * 2

        failed = [n for n, ok in (("C1无进程", c1_ok), ("C2 version空", c2_ok),
                                  ("C3超时x2", c3_ok)) if not ok]
        if not failed:
            zombies.append(base)
        else:
            suspects.append({**base, "detail": "未过判据: " + ", ".join(failed)
                             + (f"; 活体pids={pids}" if pids else "")})
    return zombies, suspects


def apply_reap(conn, zombies: list[dict], now: datetime, backup: bool = False,
               backup_ts: str | None = None) -> str | None:
    """收割: UPDATE status/error_msg/end_time, 附审计 note; 可选整表备份。永不 DELETE。

    返回备份表名 (backup=False 或零行备份时为 None)。
    """
    if backup and zombies:
        ts = backup_ts or now.strftime("%Y%m%d_%H%M%S")
        backup_table = f"pipeline_runs_backup_{ts}"
        conn.execute(f"CREATE TABLE {backup_table} AS SELECT * FROM pipeline_runs")
        conn.commit()
    else:
        backup_table = None
    for z in zombies:
        conn.execute(
            "UPDATE pipeline_runs SET status='failed', error_msg=?, end_time=? "
            "WHERE run_id=? AND status='running'",  # 二次守卫: 收割瞬间仍是 running
            (_AUDIT_NOTE, now.strftime("%Y-%m-%d %H:%M:%S"), z["run_id"]),
        )
    conn.commit()
    return backup_table


def main() -> int:
    parser = argparse.ArgumentParser(description="D-285 三判据僵尸行清理 (默认 dry-run)")
    parser.add_argument("--apply", action="store_true", help="执行收割 (默认 dry-run)")
    parser.add_argument("--backup", action="store_true", help="收割前整表备份 pipeline_runs")
    args = parser.parse_args()

    from common.database import Database
    from common.utils import bj_now

    timeout_lookup: dict[str, int] = {}
    try:
        from config_layer import cfg_get
        for name, cfg in (cfg_get("ops.pipelines") or {}).items():
            if isinstance(cfg, dict):
                timeout_lookup[name] = int(cfg.get("timeout_s", _DEFAULT_TIMEOUT_S))
    except Exception as e:
        print(f"⚠️ ops.yaml 读取失败, 全部用默认 timeout {_DEFAULT_TIMEOUT_S}s: {e}")

    db = Database()
    conn = db.get_connection()
    now_str = bj_now()
    now = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")

    zombies, suspects = select_zombies(conn, now, timeout_lookup)

    print(f"[{now_str}] running 记录: {len(zombies) + len(suspects)} "
          f"(僵尸 {len(zombies)} / suspect {len(suspects)})")
    for z in zombies:
        print(f"  🧟 {z['run_id']}  start={z['start_time']}  age={z['age_s']:.0f}s")
    for s in suspects:
        print(f"  ⏸️  {s['run_id']}  {s['detail']}")
    if not zombies:
        print("无可收割僵尸。")
        return 0
    if not args.apply:
        print("\n[dry-run] 未执行。生产执行须统筹确认时序后: --apply --backup (写库三步曲)")
        return 0

    backup_table = apply_reap(conn, zombies, now, backup=args.backup)
    if backup_table:
        n = conn.execute(f"SELECT COUNT(*) FROM {backup_table}").fetchone()[0]
        print(f"✅ 备份: {backup_table} ({n} 行)")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM pipeline_runs WHERE status='running'").fetchone()[0]
    print(f"✅ 收割 {len(zombies)} 条 → failed(附审计note); 剩余 running: {remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

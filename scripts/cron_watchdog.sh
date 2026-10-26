#!/usr/bin/env bash
# scripts/cron_watchdog.sh — P0-7 方向B: cron 看门狗包装器 (止血层)
#
# 用法 (crontab 中):
#   ... scripts/cron_watchdog.sh <key> <原命令...>
#   <key> = 管道名 (读 ops.pipelines.<key>.timeout_s)
#           或脚本入口键 (读 ops.cron_watchdog.overrides.<key>)
#
# 语义:
#   - 超时预算运行时从 config/defaults/ops.yaml 派生 (D-31 单一事实源,
#     crontab 里绝不硬编码数字; ops.yaml 改了包装器自动跟随)
#   - 总预算 = timeout_s + grace_s; 超时先 SIGTERM, --kill-after 宽限后 SIGKILL
#   - 进程死亡 = SQLite 锁随进程释放 — 08-21 ghost 持锁 6h47m 根因的止血
#   - 事件写 logs/watchdog.jsonl (铁律 #12 可观测不可静默)
#
# 红线: 这是进程外看门狗, 与"shutdown(wait=False) 掐断 ghost 线程"的老路无关;
#       杀的是整个进程 (含其全部线程), SQLite 事务保证已提交数据不损坏。
set -u

KEY="${1:-}"
if [ -z "$KEY" ]; then
    echo "usage: cron_watchdog.sh <key> <cmd...>" >&2
    exit 2
fi
shift

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"  # worktree 无 .venv → 回落系统 python3 (仅解析 yaml)
YAML="${QS_OPS_YAML:-$ROOT/config/defaults/ops.yaml}"
LOG="${QS_WATCHDOG_LOG:-$ROOT/logs/watchdog.jsonl}"
mkdir -p "$(dirname "$LOG")"

# 运行时从 ops.yaml 派生超时 (单一事实源)
# Q5/DL-65: 预算统一为 ceiling 公式, 与 schedule_check.check_budget_coupling 同源:
#   budget = timeout_s x (max_retries+1) + retry_delay_s x max_retries + grace_s
# 修复「预算横跨重试」缺陷: 此前只算 timeout_s+grace_s 一次, 而 orchestrator 内部
# 会按 max_retries 重试 (每次 retry_delay_s 间隔) — 外层预算吞不下重试, 进程在
# 重试中途被外层 timeout 处决, max_retries 形同虚设 (ddp 重试 86s/factor_update 54s
# 即被处决的实况)。ceiling 公式给足全部重试的天花板, 重试语义才真正生效。
# 非管道入口 (overrides) 无重试概念, 维持 t+grace。
OUT="$("$PY" - "$KEY" "$YAML" <<'PYEOF'
import sys
import yaml
key, path = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
ops = cfg.get("ops", {}) or {}
wd = ops.get("cron_watchdog", {}) or {}
grace = int(wd.get("grace_s", 120))
default_t = int(wd.get("default_timeout_s", 300))
pipes = ops.get("pipelines", {}) or {}
if key in pipes:
    pcfg = pipes[key]
    t = int(pcfg.get("timeout_s", default_t))
    retries = int(pcfg.get("max_retries", 0))
    delay = int(pcfg.get("retry_delay_s", 30))
    has_deps = bool(pcfg.get("depends_on"))
    # ceiling 公式 (与 schedule_check.py check_budget_coupling 同源, d5783e9):
    # 给足全部重试天花板, 修「预算横跨重试」缺陷 (重试中途被外层 timeout 处决)
    ceiling = t * (retries + 1) + delay * retries
else:
    t = int((wd.get("overrides", {}) or {}).get(key, default_t))
    has_deps = False
    ceiling = t  # 非管道入口无重试概念, 维持 t
# DL-65 统一公式 (a5a4f86 + d5783e9 并合): budget = max(ceiling, 依赖等待窗) + grace
# 带依赖管道须覆盖 wait_for_dependencies 等待窗(900s), 否则上游延迟时
# watchdog 在等待期间 SIGTERM, 进程死于落记录前 (a5a4f86 维度)。
if has_deps:
    wait_window = 900
    margin = 60  # 等待结束后的执行余量
    ceiling = max(ceiling, wait_window + margin)
print(ceiling + grace, grace)
PYEOF
)" || { echo "[cron_watchdog] 无法从 $YAML 派生 key=$KEY 的超时" >&2; exit 2; }
TOTAL_S=${OUT% *}
GRACE_S=${OUT#* }

TS="$(date '+%Y-%m-%d %H:%M:%S')"
echo "{\"event\": \"watchdog_start\", \"ts\": \"$TS\", \"key\": \"$KEY\", \"budget_s\": $TOTAL_S, \"grace_s\": $GRACE_S}" >> "$LOG"

timeout --kill-after="${GRACE_S}s" "${TOTAL_S}s" "$@"
RC=$?

TS="$(date '+%Y-%m-%d %H:%M:%S')"
if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
    # RC=124: 超预算 SIGTERM 后进程退出; RC=137: 宽限期耗尽 --kill-after SIGKILL。
    # 两者都是「进程确定死亡」强语义 (137 此前漏判, 恰是 ghost 持锁最可能的路径)。
    echo "{\"event\": \"watchdog_killed\", \"ts\": \"$TS\", \"key\": \"$KEY\", \"rc\": $RC, \"budget_s\": $TOTAL_S, \"note\": \"超预算 SIGTERM->SIGKILL, 锁随进程死亡释放\"}" >> "$LOG"
    echo "[cron_watchdog] KILLED key=$KEY budget=${TOTAL_S}s rc=$RC (SIGTERM->SIGKILL)" >&2
    # G3 收尸: 进程确定死亡 -> 立刻收 pipeline_runs 当日 running 行,
    # 不留僵尸行堵 can_run 依赖门。尽力而为, 失败不影响 watchdog 自身退出码。
    "$PY" "$(dirname "$0")/watchdog_reap.py" "$KEY" >/dev/null 2>&1         || echo "[cron_watchdog] reap failed for key=$KEY" >&2
    # Q4 锁探针: 杀后实证「锁确实随进程死亡释放」(08-21 ghost 持锁 6h47m 的显式检测)。
    # --force 绕过频率节流 (杀后验证必须真测); 三分支 healthy=0/locked=3/degraded=4。
    # locked => 探针自身发 critical 告警; 此处仅记录结果, 不改变 watchdog 退出码。
    "$PY" "$(dirname "$0")/lock_probe.py" --force >/dev/null 2>&1
    LPRC=$?
    TS="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "{\"event\": \"watchdog_lock_probe\", \"ts\": \"$TS\", \"key\": \"$KEY\", \"probe_rc\": $LPRC, \"note\": \"0=healthy 3=locked(已告警) 4=degraded\"}" >> "$LOG"
    [ "$LPRC" -eq 3 ] && echo "[cron_watchdog] LOCK STILL HELD after kill key=$KEY (lock_probe rc=3)" >&2
else
    echo "{\"event\": \"watchdog_done\", \"ts\": \"$TS\", \"key\": \"$KEY\", \"rc\": $RC}" >> "$LOG"
fi
exit "$RC"

#!/usr/bin/env bash
# scripts/flock_guard.sh — DL-52: qs.db 写锁守卫 (GO-7 已应用: 2026-08-23, 17 条 cron 带闸运行)
# Q3 (卡#40): skipped_lock 告警接线 — WARNING 级, 引用锁主 key + 持锁时长
# ①(a) (统筹裁定 sm-910a6a0e07104b0c / sm-e05dd3cfe15e4abd): per-key wait_timeout_s 等待模式
#   - ops.yaml 该 key 有 wait_timeout_s>0 → 遇锁先记 waited_lock, 再 flock -w 等待,
#     获锁记 acquired_after_wait, 超时记 wait_timeout 并 exit 99
#   - 键缺省/为 0/读取失败 → 保持 flock -n 现状语义不变 (ddp 自身入口必须走此路, 不能等自己的锁)
#   - 三类事件落 logs/watchdog.jsonl (铁律 #12 可观测不可静默); G4 依赖饿死告警链路不动
#
# 用法 (crontab 中, 插在 env 赋值之后、cron_watchdog.sh 之前):
#   ... scripts/flock_guard.sh <key> scripts/cron_watchdog.sh <key> <cmd...>
#
# 语义:
#   - 单一写锁 locks/qs_db_write.lock; 获锁成功写 sidecar (.info: key/pid/acquired_at)
#   - 非阻塞模式: 锁被占 → skipped_lock 事件 + WARNING, exit 99
#   - 等待模式: 锁被占 → waited_lock 事件, flock -w 等待; 获锁 → acquired_after_wait;
#     超时 → wait_timeout 事件 + WARNING, exit 99
#   - 用 -E 99 区分"锁冲突/等待超时"与"管道自身失败"
#
# 红线: 锁文件/sidecar 与 watchdog.jsonl 都是纯文件操作, 不碰 qs.db。
set -u

KEY="${1:-}"
if [ -z "$KEY" ]; then
    echo "usage: flock_guard.sh <key> <cmd...>" >&2
    exit 2
fi
shift

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="${QS_LOCK_FILE:-$ROOT/locks/qs_db_write.lock}"  # QS_LOCK_FILE 仅供测试隔离, 生产不设
INFO="$LOCK.info"
LOG="${QS_WATCHDOG_LOG:-$ROOT/logs/watchdog.jsonl}"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"  # worktree 无 .venv → 回落系统 python3 (仅解析 yaml)
YAML="${QS_OPS_YAML:-$ROOT/config/defaults/ops.yaml}"
mkdir -p "$(dirname "$LOG")" "$(dirname "$LOCK")"

# ①(a): 从 ops.yaml 读 per-key wait_timeout_s (键缺省/异常 → 0 → 非阻塞现状)。
# 与 cron_watchdog.sh 预算派生同源同风格 (D-31 单一事实源, 绝不硬编码)。
WAIT_S="$("$PY" - "$KEY" "$YAML" <<'PYEOF' 2>/dev/null
import sys, yaml
key, path = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    pipes = (cfg.get("ops", {}) or {}).get("pipelines", {}) or {}
    wt = (pipes.get(key, {}) or {}).get("wait_timeout_s")
    print(int(wt) if wt is not None else 0)
except Exception:
    print(0)
PYEOF
)"
WAIT_S="${WAIT_S:-0}"
case "$WAIT_S" in (*[!0-9]*|"") WAIT_S=0 ;; esac

# 获锁成功时写 sidecar (在锁内, exec 前): 锁主 key + pid + 获锁时刻
export _FLOCK_GUARD_KEY="$KEY" _FLOCK_GUARD_INFO="$INFO"
RUN_UNDER_LOCK='
    echo "{\"key\": \"$_FLOCK_GUARD_KEY\", \"pid\": $$, \"acquired_at\": $(date +%s)}" \
        > "$_FLOCK_GUARD_INFO.tmp" 2>/dev/null && mv "$_FLOCK_GUARD_INFO.tmp" "$_FLOCK_GUARD_INFO" 2>/dev/null
    exec "$@"
'

# 采集锁主信息 (sidecar 优先, fuser 兜底) → 全局 OWNER_KEY / OWNER_PID / HELD_S
collect_owner() {
    NOW_EPOCH="$(date +%s)"
    OWNER_KEY="unknown"; OWNER_PID=""; HELD_S=""
    if [ -f "$INFO" ]; then
        OWNER_KEY=$(sed -n 's/.*"key": "\([^"]*\)".*/\1/p' "$INFO" 2>/dev/null)
        OWNER_PID=$(sed -n 's/.*"pid": \([0-9]*\).*/\1/p' "$INFO" 2>/dev/null)
        ACQ=$(sed -n 's/.*"acquired_at": \([0-9]*\).*/\1/p' "$INFO" 2>/dev/null)
        # sidecar 可能过期 (锁主已死但 sidecar 残留) — 校验 pid 存活
        if [ -n "$OWNER_PID" ] && ! kill -0 "$OWNER_PID" 2>/dev/null; then
            OWNER_KEY="unknown"; OWNER_PID=""; ACQ=""
        fi
        if [ -n "${ACQ:-}" ]; then
            HELD_S=$(( NOW_EPOCH - ACQ ))
            [ "$HELD_S" -lt 0 ] && HELD_S=0
        fi
    fi
    # sidecar 缺失/过期 → fuser 兜底拿锁主 pid, 进程年龄≈持锁时长
    if [ -z "$OWNER_PID" ] && command -v fuser >/dev/null 2>&1; then
        OWNER_PID=$(fuser "$LOCK" 2>/dev/null | awk '{print $1}')
        if [ -n "${OWNER_PID:-}" ] && [ -r "/proc/$OWNER_PID/stat" ]; then
            CLK_TCK=$(getconf CLK_TCK 2>/dev/null || echo 100)
            START_TICKS=$(awk '{print $22}' "/proc/$OWNER_PID/stat" 2>/dev/null)
            UPTIME_S=$(awk '{printf "%d", $1}' /proc/uptime 2>/dev/null)
            if [ -n "$START_TICKS" ] && [ -n "$UPTIME_S" ]; then
                HELD_S=$(( UPTIME_S - START_TICKS / CLK_TCK ))
                [ "$HELD_S" -lt 0 ] && HELD_S=0
            fi
        fi
    fi
    OWNER_PID="${OWNER_PID:--1}"
    HELD_S="${HELD_S:--1}"
}

if [ "$WAIT_S" -gt 0 ]; then
    # ── ①(a) 等待模式: 先非阻塞探一次, 锁空直接进 (无等待事件); 锁被占才等待 ──
    flock -n -E 99 "$LOCK" bash -c "$RUN_UNDER_LOCK" _ "$@"
    RC=$?
    if [ "$RC" -ne 99 ]; then
        exit "$RC"   # 锁立即空闲, 透传退出码, 不产生等待事件
    fi

    # 锁被占: 记 waited_lock, 转入等待
    TS="$(date -Iseconds)"
    collect_owner
    echo "{\"event\": \"waited_lock\", \"ts\": \"$TS\", \"key\": \"$KEY\", \"wait_timeout_s\": $WAIT_S, \"lock_owner_key\": \"$OWNER_KEY\", \"lock_owner_pid\": $OWNER_PID, \"held_seconds\": $HELD_S, \"note\": \"①(a) per-key wait: lock held, waiting up to ${WAIT_S}s\"}" >> "$LOG"

    flock -w "$WAIT_S" -E 99 "$LOCK" bash -c "$RUN_UNDER_LOCK" _ "$@"
    RC=$?
    TS="$(date -Iseconds)"
    if [ "$RC" -eq 99 ]; then
        collect_owner
        echo "{\"event\": \"wait_timeout\", \"ts\": \"$TS\", \"key\": \"$KEY\", \"wait_timeout_s\": $WAIT_S, \"lock_owner_key\": \"$OWNER_KEY\", \"lock_owner_pid\": $OWNER_PID, \"held_seconds\": $HELD_S, \"note\": \"①(a) per-key wait timed out, skip-and-alert\"}" >> "$LOG"
        echo "[flock_guard] WARNING wait_timeout key=$KEY waited=${WAIT_S}s lock_owner=$OWNER_KEY(pid=$OWNER_PID) held=${HELD_S}s — 等待超时, 本次跳过" >&2
    else
        echo "{\"event\": \"acquired_after_wait\", \"ts\": \"$TS\", \"key\": \"$KEY\", \"wait_timeout_s\": $WAIT_S, \"note\": \"①(a) lock acquired after wait\"}" >> "$LOG"
    fi
    exit "$RC"
else
    # ── 非阻塞模式 (现状, 键缺省含 ddp): flock -n, 遇锁即 skip-and-alert ──
    flock -n -E 99 "$LOCK" bash -c "$RUN_UNDER_LOCK" _ "$@"
    RC=$?
    if [ "$RC" -eq 99 ]; then
        TS="$(date -Iseconds)"
        collect_owner
        echo "{\"event\": \"skipped_lock\", \"ts\": \"$TS\", \"key\": \"$KEY\", \"lock_owner_key\": \"$OWNER_KEY\", \"lock_owner_pid\": $OWNER_PID, \"held_seconds\": $HELD_S, \"note\": \"qs_db_write.lock held by another job, skip-and-alert (DL-52/卡40)\"}" >> "$LOG"
        echo "[flock_guard] WARNING skipped_lock key=$KEY lock_owner=$OWNER_KEY(pid=$OWNER_PID) held=${HELD_S}s — 本次跳过, 锁主仍在运行" >&2
    fi
    exit "$RC"
fi

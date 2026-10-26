"""G4 (2026-08-24夜): 依赖饿死告警 — can_run 因依赖未完成而 skip 且上游非 failed/success 终态。

背景: 08-24 实测一条 daily_data_pipeline 慢 → 9 条管道级联全灭, 其中 quality_check/
freshness_audit 连 skip 记录都留不下 (watchdog 预算 240s < 依赖等待窗 900s)。
alerts 表对「依赖饿死」这一最高频故障形态零覆盖 — 铁律 #12 实例。

规格: vault 逐层审计/G4告警规格+G7A取证脚本-会话D.md
触发分支判定表 (对照 orchestrator.can_run L144-180):
  - "is still running"      → 告警 (含僵尸 running, 见 G7-A)
  - "has not run today"     → 告警 (上游从未点火: CF-01 型崩溃/排程漂移)
  - "status is 'skipped'"   → 告警 (级联传播节点)
  - "failed today"          → 不新增 (OPS-07「管道失败」告警已覆盖, 防双发)
  - "dependencies not completed within Ns timeout" → 告警 (等待窗耗尽终态)

第六分支注记 (Q4, 2026-08-26): ghost 持锁 → 下游 skip → 两边同时告警的形态。
  ghost 线程 (P0-7 方向C) 超时后 cancel() 无效 → 登记 ghost_registry + critical 告警
  ("Ghost 线程: X"); 同时下游管道 can_run 判依赖 running/skipped → skip → G4 饿死告警。
  两条告警并存 = 正确行为, 非双发: ghost 告警描述"上游线程不死+锁占用",
  饿死告警描述"下游放弃执行", 主体不同语义不同。severity 不新增规则:
  ghost 已 critical, 饿死按既有规则 (warning / ≥3条或含factor_update升critical)。
  防误读: parse_starvation 对 ghost 告警的 title/detail 天然返回 None
  (不含 "dependency 'X'" 格式), 两机制互不干扰。
  实例: 08-25 alerts id45(Ghost: daily_data_pipeline) + id46(Ghost: factor_update)
  + id47/48(饿死: dev_verify_report/daily_ops_report ← evening_report skipped)。

埋点: orchestrator.run_pipeline 的 `if not deps_ok:` 分支 — skip 落库后 return 前。
盲区补偿: quality_check 型「死在 start_run 前」走不到该埋点, 由 cron_watchdog SIGTERM
时写 observability_fallback.jsonl + health_check 补扫升格 (见 scripts/g4_watchdog_fallback.py)。
"""

from __future__ import annotations

import re

# deps_reason → 是否饿死告警。与规格稿 §1.2 判定表一一对应;
# "failed today" 故意缺席 = 负向对照 #1 (防双发)。
_STARVATION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("is still running", "running"),
    ("has not run today", "not_run"),
    ("status is 'skipped'", "skipped"),
)

_TIMEOUT_RE = re.compile(r"dependencies not completed within (\d+)s timeout")
# 从 reason 提取依赖名: "dependency 'X' ..." / 无依赖名的超时合成文案取不到则空
_DEP_RE = re.compile(r"dependency '([^']+)'")


def parse_starvation(deps_reason: str) -> dict | None:
    """解析 wait_for_dependencies/can_run 的失败原因。

    返回 None = 非饿死场景 (依赖 failed / no dependencies), 不告警;
    返回 dict = {"dep": str|None, "kind": running|not_run|skipped|wait_timeout, "wait_s": int|None}
    """
    if not deps_reason:
        return None
    for pattern, kind in _STARVATION_PATTERNS:
        if pattern in deps_reason:
            m = _DEP_RE.search(deps_reason)
            return {"dep": m.group(1) if m else None, "kind": kind, "wait_s": None}
    m = _TIMEOUT_RE.search(deps_reason)
    if m:
        dep_m = _DEP_RE.search(deps_reason)
        return {
            "dep": dep_m.group(1) if dep_m else None,
            "kind": "wait_timeout",
            "wait_s": int(m.group(1)),
        }
    return None


def build_starvation_alert(
    pipeline_name: str,
    deps_reason: str,
    starved_today: list[str] | None = None,
    dep_run_info: str = "",
) -> dict:
    """按规格稿 §1.3 构造告警字段 (title/detail/severity/action)。

    severity 规则: 单条 warning; 同日 ≥3 条管道饿死或链含 factor_update → critical。
    title 含管道名 → AlertManager.should_alert 天然按管道去重 (负向对照 #3)。
    """
    info = parse_starvation(deps_reason) or {"dep": None, "kind": "unknown", "wait_s": None}
    dep = info["dep"] or "(未知上游)"
    starved_today = starved_today or []
    others = [p for p in starved_today if p != pipeline_name]

    lines = [
        f"管道 {pipeline_name} 因依赖未完成而放弃运行。",
        f"原因: {deps_reason}",
        f"类别: {info['kind']}"
        + (f" (已等 {info['wait_s']}s)" if info.get("wait_s") else ""),
    ]
    if dep_run_info:
        lines.append(f"上游最新行: {dep_run_info}")
    if others:
        lines.append(f"当日已饿死管道: {', '.join(sorted(others))}")
    detail = "\n".join(lines)

    critical = len(starved_today) >= 3 or "factor_update" in (dep, pipeline_name)
    return {
        "severity": "critical" if critical else "warning",
        "layer": "ops",
        "title": f"依赖饿死: {pipeline_name} ← {dep}",
        "detail": detail,
        "action": (
            "查上游最新 pipeline_runs 行; 若 running 超 timeout×2 无终点判僵尸 "
            "(G7-A 三判据); 排程冲突查 ops.yaml depends_on 与 crontab 对齐 (G5)"
        ),
    }

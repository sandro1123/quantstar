"""ops_layer/schedule_check.py — OPS-05 调度漂移检测

ops.yaml 是管道定义源, crontab 是触发源 — 双源人工维护必然漂移
(08-20 审计: post_market/agent_evaluate 等 5 管道无触发方,
intraday_monitor/provider_check 是幻影)。本模块对账两源:

- schedule 非 continuous 的管道: crontab 必须有 `pipeline run <name>` 行,
  缺失即漂移 (missing)
- schedule == continuous 的管道: crontab 按设计不应有行, 单独列"幻影清单"
  (phantoms) 提示 — 处置待 D-25 拍板, 只报告不删

核心函数纯逻辑 (pipelines dict + crontab 文本 → 漂移清单), 便于单测。
"""

from __future__ import annotations

import re
import subprocess

from common.logging import get_logger

logger = get_logger("ops.schedule_check")


def read_crontab() -> str:
    """读当前用户 crontab; 不可用时返回空串 (fail-safe, 由调用方判定)。"""
    try:
        proc = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10)
        return proc.stdout if proc.returncode == 0 else ""
    except Exception as e:
        logger.warning(f"crontab -l 读取失败: {e}")
        return ""


def check_schedule_drift(pipelines: dict[str, dict], crontab_text: str) -> dict:
    """对账管道定义 vs crontab 触发行。

    返回 {"missing": [name...], "phantoms": [name...], "covered": [name...]}
    - missing: 有 schedule 定义但 crontab 无触发行的非 continuous 管道
    - phantoms: schedule=continuous 的管道 (无宿主即幻影, 报告不删)
    - covered: 有触发行的管道
    """
    missing, phantoms, covered = [], [], []
    for name, cfg in sorted(pipelines.items()):
        if not isinstance(cfg, dict):
            continue
        schedule = str(cfg.get("schedule", ""))
        if schedule == "continuous":
            phantoms.append(name)
            continue
        # 匹配规则: cron 行含 `pipeline run <name>` (词边界防 morning_brief 误配 morning_brief_x)
        pattern = re.compile(rf"pipeline run {re.escape(name)}\b")
        hit = any(
            pattern.search(line) and not line.lstrip().startswith("#")
            for line in crontab_text.splitlines()
        )
        (covered if hit else missing).append(name)
    return {"missing": missing, "phantoms": phantoms, "covered": covered}


def check_orphan_pipelines(registered: list[str],
                           top_level: dict[str, dict],
                           sub_pipelines: list[str],
                           handler_hosted: list[str] | None = None) -> dict:
    """DL-51 第三方对账: data_layer 注册表 vs ops 层接线 (G53 孤立节点检出)。

    DL-33 病灶: 管线在 data_layer 注册了, 但 ops 层 (ops.yaml 顶层定义 /
    daily_data_pipeline 子管线清单) 完全不知道 → 永远不会被调度, 静默失效。
    本函数检出这类"孤立节点":

    - registered: data_layer.pipelines 注册表里的全部管线名
    - top_level:  ops.yaml 的 ops.pipelines (顶层管道定义)
    - sub_pipelines: daily_data_pipeline 的子管线清单
      (orchestrator.DAILY_SUB_PIPELINES + 条件项, 同一事实源)
    - handler_hosted: 由其他顶层 handler 内部调用的管线 (非顶层、非 daily 子管线),
      如 auction_collect 由 _run_auction_parse 调用 — 有合法宿主, 不算孤立。

    返回 {"orphans": [name...], "wired": [name...]}
    - orphans: 注册了但无任何调度宿主 → 静默失效 (DL-33 病灶)
    - wired:   有合法宿主
    """
    hosts = set(top_level.keys()) | set(sub_pipelines) | set(handler_hosted or [])
    orphans = sorted(n for n in registered if n not in hosts)
    wired = sorted(n for n in registered if n in hosts)
    return {"orphans": orphans, "wired": wired}


def format_drift_report(result: dict) -> str:
    """人类可读漂移报告。"""
    lines = ["📋 调度对账 (ops.yaml 定义 vs crontab 触发):"]
    lines.append(f"  已覆盖: {len(result['covered'])} 个管道")
    if result["missing"]:
        lines.append(f"  ❌ 漂移 (定义有/触发无): {len(result['missing'])} 个")
        for name in result["missing"]:
            lines.append(f"     - {name}")
    if result["phantoms"]:
        lines.append("  👻 幻影清单 (schedule=continuous, 无 cron/systemd 宿主即幻影):")
        for name in result["phantoms"]:
            lines.append(f"     - {name} (处置待 D-25 拍板, 只报告不删)")
    if not result["missing"] and not result["phantoms"]:
        lines.append("  ✅ 无漂移")
    return "\n".join(lines)


def run_schedule_check(pipelines: dict[str, dict] | None = None,
                       crontab_text: str | None = None) -> tuple[bool, dict]:
    """执行对账。返回 (是否无漂移, 结果 dict)。

    pipelines 缺省时从 config 读 ops.pipelines; crontab_text 缺省时读 crontab -l。
    crontab 不可读 (空) 时视为无法对账 → 返回 False 且 missing 不填充,
    结果带 "crontab_unavailable" 标记。
    """
    if pipelines is None:
        try:
            # 2026-09-13 注册面补齐卡: cfg_get 只回扁平点键标量, 段键
            # 'ops.pipelines' 天然 None → 回落 {} → 0 missing 假绿
            # (本卡 dry 实证). 段键必须 get_section 重建嵌套 dict.
            from config_layer import get_config
            cfg = get_config()
            if not cfg._loaded:
                cfg.load()
            pipelines = cfg.get_section("ops.pipelines") or {}
        except Exception as e:
            logger.warning(f"ops.pipelines 读取失败: {e}")
            pipelines = {}
    if crontab_text is None:
        crontab_text = read_crontab()
    if not crontab_text.strip():
        return False, {"missing": [], "phantoms": [], "covered": [],
                       "crontab_unavailable": True}
    result = check_schedule_drift(pipelines, crontab_text)
    return len(result["missing"]) == 0, result

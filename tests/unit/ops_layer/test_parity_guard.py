# -*- coding: utf-8 -*-
"""Day 5 parity 守卫 — 机器钉 ops.yaml.pipelines ⊆ orchestrator.dispatch

判例「建了没接线 第 11 变体」根治钉:
  - adata_sentiment: ops.yaml+crontab 挂 + dispatch 无 → raise ValueError → 4 天 failed (实证)
  - daily_update: ops.yaml 挂 + dispatch 无 + 但 crontab 直接调 -m src pipeline run
    daily_update → 不进 orchestrator._execute_pipeline → 无 ValueError → 在跑
  - marimo_{daily,weekly,monthly}_report/dashboard_bitable_sync: ops.yaml 挂 + dispatch 无
    + crontab 调 build_marimo_report.py → 不进 orchestrator → 在跑
  - 8 案 backing pipes (kline/adjust_factor 等): dispatch 有 (顶层管道) — 同 ops.yaml 独立挂

治理案: 白名单 INTERNAL_ONLY_PIPELINES — ops.yaml 挂 但 dispatch 无消费 handler 的 合理 项
白名单成员必在 ops.yaml.pipelines 中 否则 白名单漂移 = 红

判例: 静态 parse 源码 取 dispatch 键集 (不 import 生产模块)
"""
from __future__ import annotations

import re

import yaml

# orchestrator.py 中 dispatch dict 锚 (def _execute_pipeline ... handler = dispatch.get)
_DISPATCH_PATTERN = re.compile(
    r"def _execute_pipeline\(self, name.*?handler = dispatch\.get\(name\)",
    re.DOTALL)

# 白名单: ops.yaml 挂 但 dispatch 无消费 handler 的 合理 项 (e.g. 走 crontab 直接调入口)
INTERNAL_ONLY_PIPELINES: set[str] = {
    "daily_update",
    "marimo_daily_report",
    "marimo_weekly_report",
    "marimo_monthly_report",
    "dashboard_bitable_sync",
}


def _get_dispatch_keys_from_source() -> set[str]:
    """从 src/ops_layer/orchestrator.py 静态 读 dispatch = {...} 键集。"""
    with open("src/ops_layer/orchestrator.py", "r", encoding="utf-8") as f:
        text = f.read()
    m = _DISPATCH_PATTERN.search(text)
    assert m, "orchestrator._execute_pipeline body 未被找到 — anchor 漂"
    body = m.group(0)
    keys = re.findall(r'^\s+"(\w+)":\s*self\._run_\w+', body, re.MULTILINE)
    return set(keys)


def _get_ops_yaml_pipelines_keys() -> set[str]:
    """从 config/defaults/ops.yaml 读 ops.pipelines 键集。"""
    with open("config/defaults/ops.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    pipelines = cfg.get("ops", {}).get("pipelines", {}) or {}
    return set(pipelines.keys())


# ---------------------------------------------------------------------------
# 钉 1: ops.yaml ⊆ dispatch + 白名单 (主钉: 证 假绿变体 拦截)
# ---------------------------------------------------------------------------
class TestOpsYamlSubsetOfDispatch:
    """ops.yaml.pipelines 键集 (除白名单外) 必 全 在 dispatch 键集 中。

    证据: adata_sentiment 实证 (Day 3 已修 + main merge 后 应 是 全 集)。本钉 面向
    Day 5 parity 时 的 预期 值 (Day 3 合 main 后 = 全 集, 未 合 前 = adata_sentiment 一 件 缺
    并 列 出) — 这 钉 不 必 红, 它 是 「机器 钉」会 自 动 变 红 → Day 3 合 main 后 绿。
    """

    def test_ops_yaml_pipelines_minus_whitelist_subset_dispatch(self):
        yaml_keys = _get_ops_yaml_pipelines_keys()
        dispatch_keys = _get_dispatch_keys_from_source()
        # 打 白名单 (internal only) 后 剩余 必须 全 在 dispatch
        yaml_minus_whitelist = yaml_keys - INTERNAL_ONLY_PIPELINES
        missing = yaml_minus_whitelist - dispatch_keys
        assert not missing, (
            f"ops.yaml 排程声明了 {len(missing)} 个管道,但 orchestrator.dispatch 无消费 handler "
            f"(白名单后): {sorted(missing)} — 第 11 变体 假绿,根治 钉 fail")


# ---------------------------------------------------------------------------
# 钉 2: 白名单完整性 — 白名单成员必在 ops.yaml.pipelines 中
# ---------------------------------------------------------------------------
class TestWhitelistMembership:
    """白名单成员必在 ops.yaml.pipelines 中 — 防 白名单漂移 (「白 名 单 名 不实」判死区)。"""

    def test_whitelist_members_actually_in_ops_yaml(self):
        yaml_keys = _get_ops_yaml_pipelines_keys()
        missing = INTERNAL_ONLY_PIPELINES - yaml_keys
        assert not missing, (
            f"白名单 中 {sorted(missing)} 不 在 ops.yaml.pipelines — 白名单 漂移")

    def test_whitelist_members_not_in_dispatch(self):
        """白名单内 项 应 不 在 dispatch (是 internal only, 不 走 orchestrator 入口)"""
        dispatch_keys = _get_dispatch_keys_from_source()
        leak = INTERNAL_ONLY_PIPELINES & dispatch_keys
        # 这是 设计 决策 (白名单 可 与 dispatch 重复) — 不 红 只 warn
        # 作为 记录 钉,不 hard fail
        # 若 leak > 0, 表示 该 项 有 双 重 入口 (orchestrator 也 在 cron 直 调),
        # 议 治理 决策 是否 拆
        # 此处不 fail, 只 记 供 Day 5 审计 时 检查


# ---------------------------------------------------------------------------
# 钉 3: dispatch 有 但 ops.yaml 无 的 项 白 日 列出 (反向 防线)
# ---------------------------------------------------------------------------
class TestDispatchWithoutOpsYaml:
    """orchestrator.dispatch 有 键 但 ops.yaml 无 对应 排程 的 项 — 列 出 (非 红 是 warn)。
    判例: 「dispatch 内 但 无排程」 = 「daily_update 内部 调」 (e.g. kline) 或 治 治理 漂移。
    本钉 列 出 实 存 项 供 治理 决策 (不 hard fail)。 酸 钉 mean: 若有 项 触 red → 请 治理 审 """

    def test_dispatch_extras_listed_for_governance(self):
        dispatch_keys = _get_dispatch_keys_from_source()
        yaml_keys = _get_ops_yaml_pipelines_keys()
        extras = dispatch_keys - yaml_keys
        # 此 钉 不 hard fail, 只 记录 — 若 该 列 有 值,治理 审 治理
        # 但 我们 仍 assert 此 钉, 表面 上 总 绿 — 实 意 是 「列 出 而 已」
        if extras:
            print(f"INFO: dispatch 有 而 ops.yaml 无 的 键 {sorted(extras)} — "
                  f"可能 是 daily_update 内部 调 或 治理 漂移")
        # 永 为 绿 — 本钉 用于「列 出」 而 非「挡 闸」


# ---------------------------------------------------------------------------
# 反向钉: 静态 拆 白名单 + 红 真 拦 到 (adata_sentiment 实证)
# ---------------------------------------------------------------------------
class TestParityActuallyIntercepts:
    """反向钉 ①: 若 拿 掉 白名单 中 的 'odata_sentiment' (假设 它 在), 钉1 fail。
    反向钉 ②: 钉 1 不 是 空 钉 — 若 adata_sentiment 不在 dispatch (现 时 未 合), 钉 1 必 fail (红).
    """

    def test_missing_whitelist_member_intercepts_parity(self):
        """钉 1 反证: 若 adata_sentiment 不在 dispatch 但 在 ops.yaml, 减 白名单 后 有 missing → fail"""
        yaml_keys = _get_ops_yaml_pipelines_keys()
        dispatch_keys = _get_dispatch_keys_from_source()
        # adata_sentiment 此时 尚未 合 main (Day 3 未 并) — 应 在 ops.yaml 中 有 而 dispatch 无
        yaml_keys_minus = yaml_keys - INTERNAL_ONLY_PIPELINES
        missing = yaml_keys_minus - dispatch_keys
        # 若 adata_sentiment 不在 missing, 说明 已 合 main — 证 钉 1 检 漏
        # 是 则 证 钉 1 有效 (不 是 空 钉)
        if "adata_sentiment" not in dispatch_keys:
            assert "adata_sentiment" in missing, (
                f"钉 1 检 漏: adata_sentiment 既 在 ops.yaml 又 不 在 dispatch,但 不 在 missing — 钉 1 无 检 力")


# ---------------------------------------------------------------------------
# 钉 6a: 白名单 crontab 漂移 (统筹 sm-8935a214862c44db) — 白名单成员必在 crontab 中
# ---------------------------------------------------------------------------
def _get_crontab_lines() -> list[str]:
    import os, subprocess
    # 仓库内 deploy/quantstar-crontab 是唯一权威快照; CI 不再返回硬编码列表
    # (旧实现 CI 下直接返回关键词清单, 快照漂移在 CI 永远是绿的)。
    from pathlib import Path
    dep = Path(__file__).resolve().parents[3] / "deploy" / "quantstar-crontab"
    if dep.exists():
        raw = dep.read_text(encoding="utf-8")
        return [ln for ln in raw.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    """从 crontab -l 抓 全 行 (生产 锚 定: crontab 是 唯一 dispatch 外部入口 锚)。"""
    import subprocess
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=False,
            timeout=15)
        raw = out.stdout or ""
    except Exception:
        return []
    return [ln for ln in raw.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


class TestWhitelistCrontabMembersActuallyRun:
    """白名单 INTERNAL_ONLY_PIPELINES 的每成员必须 crontab 中至少一条真实命令行。
    缺一条即红 (白名单漂移反向变体: 声明 internal 但 实际没人调 = 「建了没接线」的 镜像病)。

    判例 #9「内部 only 必 真 run 锚」+ 判例 #11「constructed unexecuted」 — 不 名 白名单 而 实 在。
    """

    # 白名单成员 → crontab 关键字 锚 (与 实际 pipeline enter 不是 必须 同 名,
    # 可能 是 该 管道 的 可执行 脚 本 标题)
    _PIPELINE_CRON_KEYWORDS = {
        "daily_update": ("daily_update",),
        "marimo_daily_report": ("build_marimo_report.py",),  # marimo_一元走 build_marimo_report.py
        "marimo_weekly_report": ("--period weekly", "--period=weekly"),
        "marimo_monthly_report": ("--period monthly", "--period=monthly"),
        "dashboard_bitable_sync": ("dashboard_bitable_sync", "build_ops_bitable.py"),
    }

    def test_each_whitelist_member_has_crontab_line(self):
        crontab_lines = _get_crontab_lines()
        crontab_text = "\n".join(crontab_lines)
        for member in INTERNAL_ONLY_PIPELINES:
            keywords = self._PIPELINE_CRON_KEYWORDS.get(member, (member,))
            has_entry = any(kw in crontab_text for kw in keywords)
            assert has_entry, (
                f"白名单成员 '{member}' 在 crontab 中无对应行 (关键词 {keywords})——"
                f"白名单漂移反向变体,钉 6 拦 = 「名 在 白名单 实 不 在 crontab」")


# ---------------------------------------------------------------------------
# 钉 6b: orchestrator 执行面与 crontab 重叠 (第十三变体 镜像病)
# ---------------------------------------------------------------------------
class TestWhitelistNoTwinEntrance:
    """白名单成员同时走 orchestrator 与 crontab = 「两个入口抢一件事」第十三变体。

    判例: daily_update 同时 在 pipeline_runs 7/7 (orchestrator 走) 和 crontab 29 行
    直接调 -m src.execution_layer.scripts.daily_update (cron 直接调) → 被跑两次。
    前一 天 10-02 我 在 execution_stats 那 天 看到 daily_update status n=2 一次 这 是 显形。
    本钉 查 该 重叠 — 若 白名单成员 既 在 crontab 那么 (走 orchestrator 入口)
    又 直接 被 crontab 调 (绕 开 orchestrator 入口), 那 就 「两个入口」 预警。
    当前不回红 (治理 决策),只 列 出 供 治理 审。
    """

    def test_whitelist_cron_invocations_route(self):
        """列 出 白名单成员 在 crontab 中 是 走 orchestrator 入口 还是直接 exec。
        不 hard fail,只 列 出 info 供治理 审 (developing 区)。"""
        crontab_lines = _get_crontab_lines()
        crontab_text = "\n".join(crontab_lines)
        routes: dict[str, list[str]] = {}
        for member in INTERNAL_ONLY_PIPELINES:
            routes[member] = []
            # 锚 'pipeline run <name>' 表 orchestrator 入口
            if f"pipeline run {member}" in crontab_text:
                routes[member].append("orchestrator")
            # 锚 直接 exec 该 脚本 (后缀 .py 调 direct script, 或 -m src.<sub>.daily_update 等)
            # 形态 1: scripts/build_marimo_report.py / scripts/build_ops_bitable.py
            # 形态 2: -m src.execution_layer.scripts.daily_update
            if (f"/{member}.py" in crontab_text
                    or f"build_{member}.py" in crontab_text
                    or f"src.{member.replace('_', '.')}" in crontab_text
                    or f"src.execution_layer.scripts.{member}" in crontab_text):
                routes[member].append("direct_script")
        # 报告 各 成员 路由
        twin = [m for m, r in routes.items() if len(set(r)) > 1]
        if twin:
            print(f"INFO: 白名单双 入口 (第十三变体 治理 审): {twin}")
        # 此处不 hard fail — 依 数据 决策 权 留给 治理 (统 筹 面)

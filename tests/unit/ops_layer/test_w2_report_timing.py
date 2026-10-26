# -*- coding: utf-8 -*-
"""W2-3/W2-4 (2026-09-19): 报告/验收时点倒挂根治 — 依赖图必须物理可达。

生产实证 (pipeline_runs 全史):
  daily_ops_report 17:30 depends_on evening_report 20:00 → 自挂起每天 skipped
  "dependency 'evening_report' has not run today" (09-15~09-18 四连实证)。
  build_ops_report 消费运维统计/健康/告警面, 零消费晚报内容 → 依赖为冗余倒挂。
  e2e_verification --phase full 18:00 验收 evening_report(20:00)/ops_report(17:30
  必 skipped) → 每天 2 红假败 (e2e_verification.log 09-16~18 三连实证)。

修法 (同 W1 dev_verify 半边案的同族改锚):
  daily_ops_report depends_on 剔除 evening_report, 保留 daily_backtest。
  本文件钉死: 报告/验收链的依赖图必须时点可达 — 依赖方排程晚于被依赖方
  的声明不得存在 (判例#11: 判据执行时点 ≠ 数据到达时点的推广形态:
  排程时点倒挂 = 结构性 skipped)。
"""
import pathlib

import yaml

OPS_YAML = pathlib.Path(__file__).resolve().parents[3] / "config/defaults/ops.yaml"


def _pipelines():
    return yaml.safe_load(OPS_YAML.read_text(encoding="utf-8"))["ops"]["pipelines"]


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def test_daily_ops_report_no_evening_report_dependency():
    """W2-3: ops 日报不得依赖晚报 (晚报 20:00, 日报 17:30 = 结构性 skipped)。"""
    p = _pipelines()["daily_ops_report"]
    deps = p.get("depends_on") or []
    assert "evening_report" not in deps, (
        f"ops日报依赖晚报=时点倒挂 ({p.get('schedule')} < evening_report 20:00), "
        f"生产实证每天 skipped")
    assert "daily_backtest" in deps, "daily_backtest 15:55 早于 17:30, 依赖合法须保留"


def test_daily_ops_report_dependencies_all_earlier():
    """W2-3 推广: daily_ops_report 的所有依赖必须排程早于它 (时点可达)。"""
    pipes = _pipelines()
    p = pipes["daily_ops_report"]
    own = _minutes(p["schedule"])
    for dep in p.get("depends_on") or []:
        dep_sched = pipes[dep].get("schedule")
        assert dep_sched is not None, f"依赖 {dep} 无排程"
        assert _minutes(dep_sched) < own, (
            f"时点倒挂: daily_ops_report({p['schedule']}) 依赖 {dep}({dep_sched})")


def test_evening_report_keeps_own_dependencies():
    """晚报自身依赖 (factor_update/daily_backtest) 不动 — 本修只解倒挂不碰晚报。"""
    p = _pipelines()["evening_report"]
    assert set(p.get("depends_on") or []) == {"factor_update", "daily_backtest"}
    assert p.get("schedule") == "20:00"


def test_report_chain_no_inversion_global():
    """全局不变量: 任何管道的 depends_on 依赖方排程必须严格早于自身。

    例外白名单: 同槽对 (依赖方与自身同 HH:MM, 由 wait_for_dependencies
    顺序等待兜底, 如 premarket_brief 09:25←auction_parse 09:25) 非倒挂;
    严格大于 (晚于自身) 才是结构性 skipped。
    """
    pipes = _pipelines()
    inversions = []
    for name, p in pipes.items():
        sched = p.get("schedule")
        if not sched:
            continue
        for dep in p.get("depends_on") or []:
            d = pipes.get(dep)
            if d is None or not d.get("schedule"):
                continue
            if _minutes(d["schedule"]) > _minutes(sched):
                inversions.append(f"{name}({sched}) ← {dep}({d['schedule']})")
    assert inversions == [], "排程时点倒挂清单: " + "; ".join(inversions)

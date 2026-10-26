# -*- coding: utf-8 -*-
"""心跳覆盖面四件 (09-25 分诊实证驱动)。

背景: heartbeat_check.py 写完、配好、有 CLI, **生产零调用方** —— 第三次
「防线建了没人接线」。而且回溯实证: 即使接线, 原样也抓不到 daily_update。
四处修法 (统筹 09-25 全批):

  甲 days 过滤     — check() 忽略 days 字段, 周频腿 (days='6'/'0') 天天必假阳
  乙 crontab 同源  — 监控集只有 ops.pipelines(51条), 漏掉「scripts/* 直驱」入口
                     (daily_update/intraday_scan/e2e_verification/api_health_check)
  丙 连续 N 天     — 「当日未成功」→「连续 N 个**交易日**未成功」(N=2),
                     周末不算断 (TradingCalendar 同款口径)。冷接线告警量 18→3
  丁 载体缺失登记  — dashboard_bitable_sync 天天 rc=0 跑成功但不产 pipeline_runs 行,
                     必须显式登记 + 写明替代判据, **不得静默豁免**
"""
from datetime import datetime

import pytest

from ops_layer import heartbeat_check as HB

# 09-21 周一 ~ 09-25 周五
MON, TUE, WED, THU, FRI = ("2026-09-21", "2026-09-22", "2026-09-23",
                           "2026-09-24", "2026-09-25")


def _now(day, hhmm="23:00"):
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")


def _pipe(schedule, days=None, timeout_s=300):
    p = {"schedule": schedule, "timeout_s": timeout_s,
         "max_retries": 0, "retry_delay_s": 0}
    if days is not None:
        p["days"] = days
    return p


# ---------- 甲: days 过滤 ----------

def test_weekly_pipeline_not_flagged_on_weekday():
    """days='6'(周六) 的腿, 周四不得报超期 —— 原实现天天报 = 必假阳。"""
    pipelines = {"weekly_summary": _pipe("08:00", days="6")}
    out = HB.check(pipelines, [], _now(THU), {})
    assert [o["name"] for o in out] == [], (
        "周频腿在非其排程日被报超期 — days 过滤未生效"
    )


def test_weekday_pipeline_still_flagged_on_weekday():
    """days='1-5' 的腿, 周四照报 (不得因 days 过滤误放)。"""
    pipelines = {"daily_leg": _pipe("08:00", days="1-5")}
    out = HB.check(pipelines, [], _now(THU), {})
    assert [o["name"] for o in out] == ["daily_leg"]


def test_missing_days_field_defaults_to_every_day():
    """未声明 days 的腿保持现状语义 (每天都该跑), 不得因新逻辑漏报。"""
    pipelines = {"no_days": _pipe("08:00")}
    out = HB.check(pipelines, [], _now(THU), {})
    assert [o["name"] for o in out] == ["no_days"]


# ---------- 乙: crontab 同源 ----------

CRONTAB_SAMPLE = """\
# 注释行应被忽略
16 16 * * 1-5 cd /home/x && scripts/flock_guard.sh daily_update scripts/cron_watchdog.sh daily_update .venv/bin/python -m src.execution_layer.scripts.daily_update >> logs/x.log 2>&1
0 17 * * 1-5 cd /home/x && scripts/flock_guard.sh news_per_stock scripts/cron_watchdog.sh news_per_stock .venv/bin/python -m src pipeline run news_per_stock
45 17 * * 1-5 cd /home/x && scripts/cron_watchdog.sh dashboard_bitable_sync .venv/bin/python scripts/dashboard.py
# 30 23 * * * 已注释掉的行不算
"""


def test_crontab_parse_yields_schedule_and_days():
    """乙: 从 crontab 同源取排程元数据 (统筹裁定: 不在 ops.yaml 另补声明)。"""
    assert hasattr(HB, "schedule_entries_from_crontab"), (
        "缺 schedule_entries_from_crontab — 乙件要求监控集元数据走 crontab 同源"
    )
    ent = HB.schedule_entries_from_crontab(CRONTAB_SAMPLE)
    assert "daily_update" in ent, f"未解析出 daily_update, 实际: {sorted(ent)}"
    assert ent["daily_update"]["schedule"] == "16:16"
    assert ent["daily_update"]["days"] == "1-5"
    assert "dashboard_bitable_sync" in ent


def test_crontab_parse_ignores_comment_lines():
    ent = HB.schedule_entries_from_crontab(CRONTAB_SAMPLE)
    assert all(not k.startswith("#") for k in ent)
    assert len(ent) == 3, f"注释行未被忽略, 实际 {sorted(ent)}"


def test_daily_update_gets_flagged_once_in_monitor_set():
    """乙件目标: daily_update 纳入监控集后, 09-24 零成功行必须被抓到。

    生产实证: 09-21~24 连续四天 skipped_lock, 零 pipeline_runs 行, 零告警。
    """
    ent = HB.schedule_entries_from_crontab(CRONTAB_SAMPLE)
    out = HB.check({"daily_update": ent["daily_update"]}, [], _now(THU), {})
    assert [o["name"] for o in out] == ["daily_update"]


# ---------- 丙: 连续 N 个交易日未成功 ----------

def test_single_day_miss_not_flagged_when_n_is_two():
    """只迟到一天不报 (分诊: A 类 13 条多是单日迟到, 不是故障)。"""
    pipelines = {"leg": _pipe("08:00", days="1-5")}
    prior = {WED: [{"pipeline_name": "leg", "status": "success"}]}
    out = HB.check(pipelines, [], _now(THU), {},
                   prior_runs_by_day=prior, min_consecutive=2)
    assert out == [], "昨天成功过, 今天迟到一次不该报"


def test_two_consecutive_days_missed_is_flagged():
    pipelines = {"leg": _pipe("08:00", days="1-5")}
    prior = {WED: [{"pipeline_name": "leg", "status": "failed"}]}
    out = HB.check(pipelines, [], _now(THU), {},
                   prior_runs_by_day=prior, min_consecutive=2)
    assert [o["name"] for o in out] == ["leg"]


def test_weekend_does_not_break_the_streak():
    """统筹钉①: 「连续」按**交易日**算 —— 周五红 + 周一红 = 连续 2 天。

    若按自然日, 中间隔着周末会被误判为"断了", 漏报真故障。
    """
    pipelines = {"leg": _pipe("08:00", days="1-5")}
    # 上周五 09-18 无成功; 今天周一 09-21
    prior = {"2026-09-18": [{"pipeline_name": "leg", "status": "failed"}]}
    out = HB.check(pipelines, [], _now(MON), {},
                   prior_runs_by_day=prior, min_consecutive=2)
    assert [o["name"] for o in out] == ["leg"], (
        "周五+周一应算连续 2 个交易日 — 「连续」被按自然日算了"
    )


def test_default_min_consecutive_keeps_old_behavior():
    """缺省 min_consecutive=1 = 现状语义, 老调用方不受影响。"""
    pipelines = {"leg": _pipe("08:00", days="1-5")}
    out = HB.check(pipelines, [], _now(THU), {})
    assert [o["name"] for o in out] == ["leg"]


# ---------- 丁: 载体缺失类显式登记 ----------

def test_no_pipeline_runs_registry_exists_and_documents_alternative():
    """丁: 「跑成功但不产 pipeline_runs」的入口必须在册 + 写明替代判据。

    统筹: 「豁免清单本身就是文档, 不是黑洞」 —— 不得静默豁免。
    """
    assert hasattr(HB, "NO_PIPELINE_RUNS_ENTRIES"), (
        "缺 NO_PIPELINE_RUNS_ENTRIES 登记表 — 载体缺失类不得静默豁免"
    )
    reg = HB.NO_PIPELINE_RUNS_ENTRIES
    assert "dashboard_bitable_sync" in reg, (
        f"dashboard_bitable_sync 未登记 (它天天 watchdog_done rc=0 跑成功却零 "
        f"pipeline_runs 行), 实际在册: {sorted(reg)}"
    )
    note = reg["dashboard_bitable_sync"]
    assert "watchdog" in str(note), (
        f"登记项必须写明替代判据 (watchdog.jsonl rc=0), 实际: {note!r}"
    )


def test_registered_entry_is_not_flagged():
    """在册的载体缺失类不参与 pipeline_runs 判据, 否则永久误报。"""
    pipelines = {"dashboard_bitable_sync": _pipe("17:45", days="1-5")}
    out = HB.check(pipelines, [], _now(THU), {})
    assert out == [], (
        "在册的载体缺失类仍被 pipeline_runs 判据误报 — 丁件未生效"
    )

def test_e2e_verification_registered_with_dm_caveat():
    """e2e_verification 在册 (统筹 09-25 批): 它晚间 failed 红得对, 不是常年红。

    它按设计在「有项失败」时 exit 1, 报的正是 DDP 的 kline 断供。若按
    pipeline_runs 判据处理, 会被当成"常年红"而失去信号价值。
    且登记项必须写明**当前推送载体是 Feishu DM (无人值守确认)** ——
    DM 没人看则该链是断的, 不得静默。
    """
    reg = HB.NO_PIPELINE_RUNS_ENTRIES
    assert "e2e_verification" in reg, (
        f"e2e_verification 未登记, 实际在册: {sorted(reg)}"
    )
    note = str(reg["e2e_verification"])
    assert "Sprint3 验证" in note or "N 项" in note, (
        f"登记项必须写明替代判据 (日志尾部 N 项 M 失败行), 实际: {note[:120]!r}"
    )
    assert "Feishu" in note or "DM" in note, (
        f"登记项必须写明当前载体是 Feishu DM 且无人值守确认, 实际: {note[:120]!r}"
    )


def test_e2e_registered_entry_not_flagged():
    """在册后不得被 pipeline_runs 判据误报。"""
    pipelines = {"e2e_verification": _pipe("20:30", days="1-5")}
    out = HB.check(pipelines, [], _now(THU), {})
    assert out == [], "在册的 e2e_verification 仍被误报 — 登记未生效"

"""OPS-05: 调度漂移检测 — 对账行为边界

测 check_schedule_drift 纯逻辑:
- 定义有/crontab 无 → missing
- 词边界: morning_brief 不被 morning_brief_extra 误配
- 注释行不算触发
- continuous → phantoms 单独列
- crontab 不可读 → crontab_unavailable 标记
"""

from ops_layer.schedule_check import check_schedule_drift, format_drift_report, run_schedule_check

_PIPELINES = {
    "morning_brief": {"schedule": "08:00"},
    "post_market": {"schedule": "15:10"},
    "agent_evaluate": {"schedule": "16:20"},
    "intraday_monitor": {"schedule": "continuous"},
    "provider_check": {"schedule": "continuous"},
}

_CRONTAB = """
# QuantStar 定时任务
0 8 * * 1-5 cd /qs && PYTHONPATH=src python -m src pipeline run morning_brief >> logs/pipeline.log 2>&1
# 0 8 * * 1-5 python -m src pipeline run post_market  (注释行不算触发)
"""


def test_missing_detected():
    result = check_schedule_drift(_PIPELINES, _CRONTAB)
    assert result["missing"] == ["agent_evaluate", "post_market"]
    assert result["covered"] == ["morning_brief"]


def test_phantoms_listed_separately():
    result = check_schedule_drift(_PIPELINES, _CRONTAB)
    assert result["phantoms"] == ["intraday_monitor", "provider_check"]
    # continuous 不进 missing
    assert "intraday_monitor" not in result["missing"]


def test_word_boundary_no_prefix_mismatch():
    """morning_brief 不被 morning_brief_extra 行误配为已覆盖。"""
    pipelines = {"morning_brief": {"schedule": "08:00"}}
    crontab = "0 8 * * 1-5 python -m src pipeline run morning_brief_extra\n"
    result = check_schedule_drift(pipelines, crontab)
    assert result["missing"] == ["morning_brief"]


def test_commented_line_not_counted():
    pipelines = {"post_market": {"schedule": "15:10"}}
    crontab = "# 10 15 * * 1-5 python -m src pipeline run post_market\n"
    result = check_schedule_drift(pipelines, crontab)
    assert result["missing"] == ["post_market"]


def test_crontab_unavailable_flagged():
    ok, result = run_schedule_check(pipelines=_PIPELINES, crontab_text="")
    assert not ok
    assert result.get("crontab_unavailable") is True


def test_no_drift_passes():
    pipelines = {"morning_brief": {"schedule": "08:00"}}
    crontab = "0 8 * * 1-5 python -m src pipeline run morning_brief\n"
    ok, result = run_schedule_check(pipelines=pipelines, crontab_text=crontab)
    assert ok
    assert result["missing"] == []


def test_report_format_lists_missing_and_phantoms():
    result = check_schedule_drift(_PIPELINES, _CRONTAB)
    text = format_drift_report(result)
    assert "post_market" in text
    assert "agent_evaluate" in text
    assert "intraday_monitor" in text
    assert "幻影" in text

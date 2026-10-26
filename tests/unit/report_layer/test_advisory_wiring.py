"""卡VII 接线红绿测试 — 三时点投顾进管线: 晨/午 section 编排 + 尾盘 14:52 管道。

依据: 统筹裁定 sm-c525b45463d94435 裁定①③ —
- 晨/午投顾段挂进 MorningBriefBuilder/MiddayBriefBuilder 的 sections() 列表;
- 尾盘投顾走独立管道 tail_advisory (14:52 非整点), report_layer build+push
  同链 (qscard 不动)。

红绿说明:
  - 移除 morning_brief/midday_brief 编排中的投顾段 → 测试 1/2 失败。
  - 移除 ReportService.build_tail_advisory → 测试 4 ImportError/False。
  - 移除 ops.yaml tail_advisory 条目 → 测试 5 失败。
  - 移除 orchestrator dispatch 表的 _run_tail_advisory → 测试 6 失败。

hermetic: 尾盘段渲染用内存库, 不碰生产。
"""
import inspect
import os
import sqlite3
from unittest.mock import MagicMock

import yaml

from report_layer.reports.base import ReportContext
from report_layer.sections.advisory_brief import AdvisoryTailSection

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


# ---- 1. 晨报编排含投顾晨段 (指引前置: risk_compliance 之前) ----
def test_morning_brief_includes_advisory_section():
    from report_layer.reports.morning_brief import MorningBriefBuilder
    names = [s.name for s in MorningBriefBuilder().sections()]
    assert "advisory_morning" in names, "晨报缺投顾晨段"
    assert names.index("advisory_morning") < names.index("risk_compliance")


# ---- 2. 午报编排含投顾午段 (候选跟踪段之后) ----
def test_midday_brief_includes_advisory_section():
    from report_layer.reports.midday_brief import MiddayBriefBuilder
    names = [s.name for s in MiddayBriefBuilder().sections()]
    assert "advisory_midday" in names, "午报缺投顾午段"
    assert names.index("advisory_midday") > names.index("midday_candidate_tracking")


# ---- 3. 时点互斥: 尾盘段不得混入晨/午管线 ----
def test_tail_excluded_from_morning_and_midday():
    from report_layer.reports.morning_brief import MorningBriefBuilder
    from report_layer.reports.midday_brief import MiddayBriefBuilder
    m = [s.name for s in MorningBriefBuilder().sections()]
    d = [s.name for s in MiddayBriefBuilder().sections()]
    assert "advisory_tail" not in m, "尾盘段不得混入晨报"
    assert "advisory_tail" not in d, "尾盘段不得混入午报"


# ---- 4. ReportLayer 有尾盘投顾 build 方法 ----
def test_report_service_has_build_tail_advisory():
    from report_layer import ReportLayer
    assert hasattr(ReportLayer, "build_tail_advisory"), \
        "ReportLayer 缺 build_tail_advisory"


# ---- 5. ops.yaml tail_advisory 条目 (14:52 非整点, 工作日, 无依赖) ----
def test_ops_yaml_tail_advisory_entry():
    yaml_path = os.path.join(_ROOT, "config", "defaults", "ops.yaml")
    with open(yaml_path) as f:
        cfg = yaml.safe_load(f)
    pipelines = cfg.get("ops", {}).get("pipelines", {})
    assert "tail_advisory" in pipelines, "ops.yaml 缺 tail_advisory 管道条目"
    entry = pipelines["tail_advisory"]
    assert entry.get("schedule") == "14:52", "尾盘投顾排程须为 14:52 (非整点)"
    assert entry.get("days") == "1-5", "尾盘投顾仅工作日"
    assert entry.get("depends_on", []) == [], "尾盘投顾无依赖 (只读, 不等数据链)"
    assert entry.get("timeout_s", 0) >= 60, "超时预算至少 60s"


# ---- 6. orchestrator dispatch 表有 handler ----
def test_orchestrator_dispatch_tail_advisory():
    from ops_layer.orchestrator import PipelineOrchestrator
    src = inspect.getsource(PipelineOrchestrator._execute_pipeline)
    assert '"tail_advisory": self._run_tail_advisory' in src, \
        "dispatch 表缺少 tail_advisory handler"
    assert hasattr(PipelineOrchestrator, "_run_tail_advisory")


# ---- 7. 尾盘段单独可渲染 (独立管道, 不搭晨/午) ----
def test_tail_section_renders_standalone():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT,
            market TEXT, list_date TEXT, is_active INTEGER, delist_date TEXT);
        CREATE TABLE model_predictions (date TEXT, code TEXT, ml_score REAL,
            factor_score REAL, blended_score REAL);
        CREATE TABLE stock_pool (code TEXT, name TEXT, status TEXT, entry_price REAL,
            last_price REAL, position_qty INTEGER, unrealized_pnl_pct REAL,
            holding_days INTEGER, stop_loss REAL, dynamic_stop_loss REAL,
            take_profit REAL, entry_date TEXT, exit_reason TEXT);
        CREATE TABLE daily_regime (date TEXT, regime TEXT);
        CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL);
        INSERT INTO stock_pool VALUES ('688349','昊海生科','active',29.0,
            30.5,800,5.17,3,28.0,28.5,32.0,'2026-08-28',NULL);
    """)
    conn.commit()
    data = MagicMock()
    db = MagicMock()
    db.get_connection.return_value = conn
    data.db = db
    ctx = ReportContext(date="2026-08-31", data_layer=data)
    result = AdvisoryTailSection().build(ctx)
    assert result is not None
    assert "688349" in result.body
    conn.close()

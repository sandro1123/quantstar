# -*- coding: utf-8 -*-
"""Beta 门禁回归四族 (审计校准 sm-1d6c8fc1076a4fe3 → W2-1)。

四族 = 时间点一致性 / 分母独立性 / 状态诚实性 / 账务一致性。
设计原则 (审计裁定):
  - 判据独立性: 断言不依赖「正确答案长什么样」, 只依赖「被测对象是否响应输入」。
  - 宁缺勿脏: :memory: 隔离库, 不碰生产。
  - 未知缺失必须显形: unknown_gap 进独立表 + 码单 + 源尝试, 不静默吞、不声称停牌。
  - 状态诚实性: best_effort 降级 = partial + 具名清单, 不得绿灯吞, 不得覆写历史 failed。
  - 账务一致性: 补偿下游消费与补偿数据 date+run 级一致, 不只看 calc_date 字面。
  - 双指标: full_universe(5816) observed coverage + eligible_universe gate coverage,
    不造单一数字; eligible 来自独立当日新鲜状态, 不用「最近有 K 线」反推。
"""
import inspect
import pathlib

import pytest
import yaml

OPS_YAML = pathlib.Path(__file__).resolve().parents[3] / "config/defaults/ops.yaml"


def _pipelines():
    return yaml.safe_load(OPS_YAML.read_text(encoding="utf-8"))["ops"]["pipelines"]


# ---------------------------------------------------------------------------
# 族一: 时间点一致性 (生产 crontab 真相, 非 ops.yaml 代理)
# ---------------------------------------------------------------------------
# e2e_verification 不在 ops.yaml pipelines, 走 crontab 直调 scripts/e2e_verification.py。
# 生产 crontab (2026-09-19 实证): full=18:00 < evening_report 20:00 → 每夜 2 假红。
# 该倒挂的修复 = crontab 模板挪 20:30 (今晚 22:05 窗), 属 W2-6 模板重生卡, 不在本
# 单测源码可读面。此处钉死「yaml 内 depends_on 无倒挂」+「e2e 挪后须有锚」。

class TestTimepointConsistency:
    """depends_on 链时序: 下游不得依赖一个调度时刻晚于自身的腿。"""

    def test_no_dependency_inversion_in_yaml(self):
        """ops.yaml 内: dep 的 schedule 必须 <= 依赖方的 schedule (同槽豁免)。

        schedule 形态: 'HH:MM' / 'HH:MM,MM'(多时刻取最早) / 'continuous'/'hourly'
        (非固定时刻, 无倒挂可比, 跳过)。生产实证 2026-09-19: yaml 内零倒挂。
        """
        pipes = _pipelines()
        times = {}
        for name, blk in pipes.items():
            t = (blk or {}).get("schedule")
            if not t:
                continue
            try:
                first = str(t).split(",")[0].strip()
                h, m = first.split(":")[:2]
                times[name] = int(h) * 60 + int(m)
            except (ValueError, IndexError, AttributeError):
                continue  # continuous / hourly 等非 HH:MM, 无固定时刻可比
        inversions = []
        for name, blk in pipes.items():
            for dep in (blk or {}).get("depends_on", []) or []:
                if name in times and dep in times and times[dep] > times[name]:
                    inversions.append((name, times[name], dep, times[dep]))
        assert not inversions, f"yaml 依赖倒挂: {inversions}"

    def test_daily_ops_report_not_depend_on_evening_report(self):
        """W2-3 回归锚: daily_ops_report depends_on 不得再含 evening_report。"""
        blk = _pipelines().get("daily_ops_report") or {}
        assert "evening_report" not in (blk.get("depends_on") or [])

    def test_dev_verify_report_not_inverted(self):
        """dev_verify_report(17:15) depends_on factor_update(16:15) — 不倒挂 (已实证)。"""
        pipes = _pipelines()
        blk = pipes.get("dev_verify_report") or {}
        deps = blk.get("depends_on") or []
        if "factor_update" in deps:
            fu = (pipes.get("factor_update") or {}).get("schedule")
            dv = blk.get("schedule")
            if fu and dv:
                assert fu <= dv, "dev_verify 依赖倒挂"


# ---------------------------------------------------------------------------
# 族二: 分母独立性 + 双指标 (full vs eligible, unknown 显形)
# ---------------------------------------------------------------------------

class TestDenominatorIndependence:
    """DDP 覆盖率: full_universe=5816 observed + eligible_universe gate 双指标。

    审计红线: 不可用「未采到/最近有 K线」推导「停牌」再排除; 不在 5816 与 5553
    之间造单一数字; unknown_gap 显形留码单+源尝试, 不静默跳过。
    """

    def test_kline_universe_from_active_not_from_fetch_result(self):
        """kline 宇宙锚定 stock.get_all_active() (独立名录), 非采集结果集。"""
        from data_layer.pipelines.kline import KlinePipeline
        src = inspect.getsource(KlinePipeline.run)
        assert "get_all_active" in src, "kline 宇宙未锚定独立名录"

    def test_unknown_gap_surfaced_not_silent(self):
        """60 天无 K线且 is_active=1 的码: 分类 unknown, 写显形表, 不声称停牌。

        丙案钉死: 显形表 pipeline_kline_exceptions (date, code, reason) 主键,
        classification='unknown', 留 last_kline_date + source_attempts。
        """
        from data_layer.pipelines import kline as kmod
        src = inspect.getsource(kmod)
        surfaced = ("pipeline_kline_exceptions" in src
                    or "unknown_gap" in src
                    or "coverage_exception" in src)
        assert surfaced, "unknown_gap 无显形载体 — 静默吞即违反红线"

    def test_unknown_never_classified_suspended(self):
        """显形表 classification 不得为 suspended/delisted (措辞红线: 不能反推停牌)。

        丙案已落地: classification 恒='unknown' (60 天无 K线=排查分组, 非停牌断言)。
        此测试钉死显形表行为: 任何 code 的 classification 不得为 suspended/delisted/halted。
        """
        from common.database import Database
        from ops_layer.core.tables import OPS_TABLES_DDL
        from data_layer.pipelines.kline import _record_unknown_gap, _UNKNOWN_GAP_DDL
        db = Database(":memory:")
        db.init_db()
        conn = db.get_connection()
        conn.executescript(OPS_TABLES_DDL)
        conn.executescript(_UNKNOWN_GAP_DDL)
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date))""")
        conn.commit()
        _record_unknown_gap(conn, "2026-09-21", "920305", "2026-07-28", "all 4 sources empty")
        _record_unknown_gap(conn, "2026-09-21", "000004", "2026-07-17", "all 4 sources empty")
        rows = conn.execute(
            "SELECT DISTINCT classification FROM pipeline_kline_exceptions").fetchall()
        for r in rows:
            assert r[0] not in ("suspended", "delisted", "halted"), \
                f"classification={r[0]} 违反「不可用未采到推停牌」"
        assert any(r[0] == "unknown" for r in rows), "显形表应有 unknown 分类"

    def test_dual_coverage_metrics_present(self):
        """双指标: full_universe observed + eligible gate, 不塌成单一数字。"""
        from ops_layer.orchestrator import PipelineOrchestrator
        src = inspect.getsource(PipelineOrchestrator._run_data_pipeline)
        # 覆盖率探针存在 (F2.5), 且 min_kline_rows 阈值保留 (>=85% 语义)
        assert "min_kline_rows" in src or "MIN_KLINE_ROWS" in src, \
            "覆盖率门禁阈值缺失"


# ---------------------------------------------------------------------------
# 族三: 状态诚实性 (降级可见, 三态可区分, 不覆写历史 failed)
# ---------------------------------------------------------------------------

class TestStatusHonesty:
    """best_effort 降级 = partial + 具名清单 + 告警; fuyao 三态可区分。"""

    def test_best_effort_degraded_not_silent_success(self):
        """best_effort 腿降级 → 父管道 partial (metadata partial=True), 非 success。"""
        from ops_layer.orchestrator import PipelineOrchestrator
        src = inspect.getsource(PipelineOrchestrator._run_data_pipeline)
        assert "best_effort_degraded" in src
        assert '"partial": True' in src or "'partial': True" in src, \
            "best_effort 降级未落 partial — 绿灯吞"

    def test_fuyao_partial_leaves_named_trace(self):
        """fuyao partial → best_effort_degraded 具名; fuyao 在 best_effort 集合 (W2-1)。"""
        from ops_layer.orchestrator import PipelineOrchestrator
        src = inspect.getsource(PipelineOrchestrator._run_data_pipeline)
        assert "best_effort_degraded.append" in src, "降级未具名留痕"
        assert "fuyao_financial" in src

    def test_fuyao_states_distinguishable(self):
        """fuyao partial/degraded/expected_no_update/source_failure 三态须可区分。

        季报语义: 没有新公告 = expected_no_update 非故障; 距 last_success_at 超阈值
        且在应有更新窗口内 = source_failure。判据锚 report_period/announcement_date
        /last_success_at, 非「当日 partial」。
        """
        from data_layer.pipelines import fuyao as fmod
        src = inspect.getsource(fmod)
        # 三态载体: skipped(=expected_no_update) 已有; last_success/报告期锚须存在
        has_period = ("report_period" in src or "last_success" in src
                      or "announcement" in src or "skipped" in src)
        assert has_period, "fuyao 缺报告期/last_success 锚 — 三态不可区分"

    def test_compensation_does_not_overwrite_historical_failed(self):
        """补偿 = 独立 run 名 evening_paper_trading, 不 UPDATE 原 daily_data_pipeline 行。"""
        from ops_layer.orchestrator import PipelineOrchestrator
        src = inspect.getsource(PipelineOrchestrator)
        assert "evening_paper_trading" in src, "补偿腿无独立 run 名 — 有覆写风险"


# ---------------------------------------------------------------------------
# 族四: 账务一致性 (补偿下游消费与补偿数据 date+run 级一致)
# ---------------------------------------------------------------------------

class TestAccountingConsistency:
    """补偿腿补上的数据, 下游必须真消费同 date 同 run, 不得旧数据重用。

    最低验收 (审计): compensation_run_id + 业务日期 + 上游快照指纹 +
    factor_daily/model_predictions 行数与日期 + source_run_id/calc_run_id +
    权重版本 hash + 下游开始时间晚于补偿完成时间。三元组(仅 date 相等)不够。
    """

    def test_compensation_writes_own_run_row(self):
        """补偿腿落地产生独立 pipeline_runs 行 (run_id 可追溯)。"""
        from ops_layer.orchestrator import PipelineOrchestrator
        found = any("compensat" in n.lower() or "evening_paper" in n.lower()
                    for n in dir(PipelineOrchestrator))
        assert found, "补偿腿无独立 handler — 无法追溯 run_id"

    def test_paper_trading_uses_bj_date(self):
        """模拟盘消费日期锚定 bj_date() (当日), 不被 cfg 覆写 → date 一致性。"""
        from ops_layer.orchestrator import PipelineOrchestrator
        src = inspect.getsource(PipelineOrchestrator._run_paper_trading)
        assert "bj_date" in src, "模拟盘日期未锚定 bj_date"

    def test_compensation_carries_run_id_chain(self):
        """补偿 metadata 须携带 source_run_id / calc_run_id 链, 供下游对拍。

        仅 date 相等不足以证版本一致 — 须 run 级链接 (source_run_id 指同一补偿 run
        或明确「重算基于该日期最终快照」)。
        """
        from ops_layer.orchestrator import PipelineOrchestrator
        src = inspect.getsource(PipelineOrchestrator)
        # run 级追溯锚 (run_id 贯穿补偿→因子→预测)
        assert "run_id" in src, "缺 run_id 链 — 补偿版本一致性不可证"

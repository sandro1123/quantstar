"""Report Layer — 报告编排"""

from __future__ import annotations

from common.decorators import catch_quantstar_error, with_timeout
from common.logging import get_logger
from config_layer import Config
from delivery_layer.message import Message
from report_layer.formatters.feishu_card import FeishuCardFormatter
from report_layer.formatters.text import TextFormatter
from report_layer.reports.base import ReportBuilder, ReportContext
from report_layer.reports.evening_report import EveningReportBuilder
from report_layer.validator import ContentValidator

logger = get_logger("report")


class ReportLayer:
    def __init__(self, config: Config | None = None, data_layer: object | None = None,
                 strategy_layer: object | None = None, execution_layer: object | None = None,
                 agent_layer: object | None = None, backtest_layer: object | None = None,
                 ops_layer: object | None = None,
                 llm_service: object | None = None):
        self.config = config
        self.data_layer = data_layer
        self.strategy_layer = strategy_layer
        self.execution_layer = execution_layer
        self.agent_layer = agent_layer
        self.backtest_layer = backtest_layer
        self.ops_layer = ops_layer
        self.llm_service = llm_service
        self.formatters = {
            "feishu_card": FeishuCardFormatter(),
            "text": TextFormatter(),
        }
        self._active_formatter = "feishu_card"
        self.validator = ContentValidator()

    @property
    def formatter(self):
        return self.formatters.get(self._active_formatter, self.formatters["feishu_card"])

    @formatter.setter
    def formatter(self, value):
        if isinstance(value, str) and value in self.formatters:
            self._active_formatter = value
        elif hasattr(value, "format"):
            self.formatters[value.__class__.__name__] = value
            self._active_formatter = value.__class__.__name__

    def use_text_fallback(self):
        self._active_formatter = "text"

    @catch_quantstar_error(layer_name="report")
    # B9 补 (2026-10-21): _build_report 是**所有报告腿的公共内层**,
    # 原硬编码 60s 会把外层放宽的预算照截 (今早 08:00 日志三超时并列之二)。
    # 改为装配期由 build_* 外层按各自管线设置 self._build_budget_key,
    # 无设置时回落 morning_brief 键 (晨报是最长的一条链), 再回落 180s 字面量。
    @with_timeout(timeout_s=180, config_key="ops.pipelines.morning_brief.timeout_s")
    def _build_report(self, builder: ReportBuilder, date: str, report_mode: str = "debug") -> Message | None:
        ctx = ReportContext(
            date=date, data_layer=self.data_layer,
            strategy_layer=self.strategy_layer, execution_layer=self.execution_layer,
            agent_layer=self.agent_layer, backtest_layer=self.backtest_layer,
            ops_layer=self.ops_layer, llm_service=self.llm_service,
            report_mode=report_mode,
        )
        message = builder.build(ctx)
        if message:
            issues = self.validator.validate(message.content)
            if issues:
                from common.logging import get_logger
                get_logger("report").warning(f"Content validation: {len(issues)} issues")
            # 判例#166: 加工者就地覆盖原字段等于丢失; formatter 会覆盖 content, 先 stash 原形.
            # (Day5: _log_sections 读 raw_content 取 sections, 保 channel 读飞书卡).)
            message.raw_content = dict(message.content)
            message.content = self.formatter.format(message)
        return message

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=180, config_key="ops.pipelines.evening_report.timeout_s")
    def build_evening_report(self, date: str, report_mode: str = "debug") -> Message | None:
        return self._build_report(EveningReportBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=180, config_key="ops.pipelines.morning_brief.timeout_s")
    def build_morning_brief(self, date: str, report_mode: str = "debug") -> Message | None:
        # B9 knife2: 预算读配置（ops.yaml timeout_s=180），不再写死 60s。
        # 无键/非法值回落 180s 字面量。其它报告腿不动。
        from report_layer.reports.morning_brief import MorningBriefBuilder
        return self._build_report(MorningBriefBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=180, config_key="ops.pipelines.midday_brief.timeout_s")
    def build_midday_brief(self, date: str, report_mode: str = "debug") -> Message | None:
        from report_layer.reports.midday_brief import MiddayBriefBuilder
        return self._build_report(MiddayBriefBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=180, config_key="ops.pipelines.premarket_brief.timeout_s")
    def build_premarket_brief(self, date: str, report_mode: str = "debug") -> Message | None:
        """报告宪法 #7: 盘前速报 09:25 — 集合竞价扫描昨日推荐+观察池。"""
        from report_layer.reports.premarket_brief import PremarketBriefBuilder
        return self._build_report(PremarketBriefBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=180, config_key="ops.pipelines.tail_advisory.timeout_s")
    def build_tail_advisory(self, date: str, report_mode: str = "debug") -> Message | None:
        """卡VII: 尾盘投顾 (14:52) — 独立管道, 只读, qscard 不动。"""
        from report_layer.reports.tail_advisory import TailAdvisoryBuilder
        return self._build_report(TailAdvisoryBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=180, config_key="ops.pipelines.morning_review.timeout_s")
    def build_morning_review(self, date: str, report_mode: str = "debug") -> Message | None:
        from report_layer.reports.morning_review import MorningReviewBuilder
        return self._build_report(MorningReviewBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=180, config_key="ops.pipelines.daily_ops_report.timeout_s")
    def build_ops_report(self, date: str, report_mode: str = "debug") -> Message | None:
        from report_layer.reports.ops_report import OpsReportBuilder
        return self._build_report(OpsReportBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=180, config_key="ops.pipelines.dev_verify_report.timeout_s")
    def build_dev_verify(self, date: str, report_mode: str = "debug") -> Message | None:
        from report_layer.reports.dev_verify import DevVerifyBuilder
        return self._build_report(DevVerifyBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    # 本腿**无独立管道宿主** (由 orchestrator:2254 的 _run_intraday_monitor
    # 驱动, 非独立管道), 故不接 config_key —— 接一个没有宿主的键 = 造一个
    # 没人读的配置 (C 卡实测: 原挂 ops.pipelines.intraday_alert 该键不存在,
    # 属假接线: 运维改配置不生效)。预算固定 180s; 若将来它有了独立管道, 再补键。
    # 未改挂 intraday_monitor.timeout_s 的原因: 后者=30s 是监控**循环**预算,
    # 挂上会把本腿从 180s 截到 30s (真回退) 且语义错位。
    @with_timeout(timeout_s=180)
    def build_intraday_alert(self, date: str, report_mode: str = "debug") -> Message | None:
        from report_layer.reports.intraday_alert import IntradayAlertBuilder
        return self._build_report(IntradayAlertBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    # 本腿**无独立管道宿主** (唯一调用方是 scripts/build_midday_brief.py:263
    # 直调, 不经 ops 调度), 故不接 config_key —— 接一个没有宿主的键 = 造一个
    # 没人读的配置 (C 卡实测: 原挂 ops.pipelines.alerts 该键不存在, 属假接线)。
    # 预算固定 180s; 若将来它有了独立管道, 再补键。
    @with_timeout(timeout_s=180)
    def build_alerts(self, date: str, report_mode: str = "debug") -> Message | None:
        from report_layer.reports.alerts import AlertBuilder
        return self._build_report(AlertBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=180, config_key="ops.pipelines.weekly_summary.timeout_s")
    def build_weekly_summary(self, date: str, report_mode: str = "debug") -> Message | None:
        from report_layer.reports.weekly_summary import WeeklySummaryBuilder
        return self._build_report(WeeklySummaryBuilder(), date, report_mode)

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=10)
    def health(self):
        from ops_layer.core.models import HealthCheck, LayerHealth
        checks = []

        # 1. Formatter readiness
        try:
            fmt_count = len(self.formatters)
            checks.append(HealthCheck(name="formatters", status="pass",
                detail=f"格式化器就绪 ({self._active_formatter}, 共{fmt_count}个)"))
        except Exception as e:
            checks.append(HealthCheck(name="formatters", status="fail", detail=f"格式化器异常: {e}"))

        # 2. Dependencies availability
        dl_ok = self.data_layer is not None
        sl_ok = self.strategy_layer is not None
        el_ok = self.execution_layer is not None
        al_ok = self.agent_layer is not None
        deps_detail = f"Data={'✓' if dl_ok else '✗'} Strategy={'✓' if sl_ok else '✗'} Exec={'✓' if el_ok else '✗'} Agent={'✓' if al_ok else '✗'}"
        deps_status = "pass" if dl_ok else "warn"
        checks.append(HealthCheck(name="dependencies", status=deps_status, detail=deps_detail))

        # 3. Validator status
        try:
            validator_ok = self.validator is not None if hasattr(self, "validator") else False
            checks.append(HealthCheck(name="validator", status="pass" if validator_ok else "warn",
                detail="内容校验器就绪" if validator_ok else "内容校验器未加载"))
        except Exception as e:
            checks.append(HealthCheck(name="validator", status="warn", detail=f"校验器异常: {e}"))

        # Overall
        status = "green"
        for c in checks:
            if c.status == "fail":
                status = "red"
                break
            elif c.status == "warn":
                status = "yellow"
        return LayerHealth(layer="report", status=status, checks=checks,
            summary=f"Report Layer {'正常' if status == 'green' else '需关注'}")

    @catch_quantstar_error(layer_name="report")
    @with_timeout(timeout_s=10)
    def shutdown(self, timeout: float = 5.0):
        logger.info("Report Layer 已刷新待处理报告")
        return True

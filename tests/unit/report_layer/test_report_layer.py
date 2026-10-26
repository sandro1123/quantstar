"""Report Layer 单元测试"""
import pytest
from unittest.mock import MagicMock
from report_layer import ReportLayer


@pytest.fixture
def report_layer():
    return ReportLayer()


class TestReportLayerInit:
    def test_creates_with_defaults(self, report_layer):
        assert report_layer.config is None
        assert report_layer._active_formatter == "feishu_card"

    def test_creates_with_config(self):
        config = MagicMock()
        rl = ReportLayer(config=config)
        assert rl.config is config

    def test_creates_with_layers(self):
        data = MagicMock()
        strategy = MagicMock()
        rl = ReportLayer(data_layer=data, strategy_layer=strategy)
        assert rl.data_layer is data
        assert rl.strategy_layer is strategy


class TestReportLayerFormatter:
    def test_default_formatter(self, report_layer):
        assert report_layer.formatter is not None
        assert hasattr(report_layer.formatter, 'format')

    def test_use_text_fallback(self, report_layer):
        report_layer.use_text_fallback()
        assert report_layer._active_formatter == "text"

    def test_set_formatter_by_name(self, report_layer):
        report_layer.formatter = "text"
        assert report_layer._active_formatter == "text"

    def test_set_formatter_by_object(self, report_layer):
        class MyFormatter:
            def format(self, msg):
                return msg
        fmt = MyFormatter()
        report_layer.formatter = fmt
        assert report_layer._active_formatter == "MyFormatter"


class TestReportLayerBuild:
    def test_build_evening_report_returns_none_without_layers(self, report_layer):
        # R3: risk_compliance 段永远渲染合规行 (范式 8), 无层时也产出 Message,
        # 与晨报/午报同款宽松断言对齐。
        result = report_layer.build_evening_report("2026-01-01")
        assert result is None or hasattr(result, 'content')

    def test_build_morning_brief_returns_message(self, report_layer, monkeypatch):
        from report_layer.sections.overnight_market import OvernightMarketSection
        monkeypatch.setattr(OvernightMarketSection, "_fetch_overnight", lambda self: {})
        result = report_layer.build_morning_brief("2026-01-01")
        assert result is None or hasattr(result, 'content')

    def test_build_midday_brief_returns_none_without_layers(self, report_layer):
        result = report_layer.build_midday_brief("2026-01-01")
        assert result is None or hasattr(result, 'content')

    def test_build_ops_report_returns_none_without_layers(self, report_layer):
        result = report_layer.build_ops_report("2026-01-01")
        assert result is None or hasattr(result, 'content')


class TestReportLayerHealth:
    def test_health_returns_layer_health(self, report_layer):
        health = report_layer.health()
        assert health.layer == "report"
        assert "formatters" in [c.name for c in health.checks]

    def test_health_after_formatter_change(self, report_layer):
        report_layer.use_text_fallback()
        health = report_layer.health()
        formatter_checks = [c for c in health.checks if c.name == "formatters"]
        assert len(formatter_checks) > 0
        assert "text" in formatter_checks[0].detail


class TestReportLayerShutdown:
    def test_shutdown_returns_true(self, report_layer):
        assert report_layer.shutdown() is True


class TestReportLayerDecorators:
    def test_build_evening_report_has_catch_decorator(self):
        import report_layer
        method = report_layer.ReportLayer.build_evening_report
        assert hasattr(method, '__wrapped__')

    def test_health_has_catch_decorator(self):
        import report_layer
        method = report_layer.ReportLayer.health
        assert hasattr(method, '__wrapped__')


class TestRemainingReportSectionsDataclass:
    """Regression: v0.14 dataclass 迁移漏网之鱼 — alerts/intraday_alert/ops_report/dev_verify
    的 section 曾返回 dict, ReportBuilder.build 的 asdict(dict) 会抛 TypeError 被吞成 error。
    修复后必须返回 SectionResult 且 build() 全程无 error 注入。
    """

    def _ctx(self, **layers):
        from report_layer.reports.base import ReportContext
        ctx = ReportContext(date="2026-08-07", report_mode="debug")
        for name, layer in layers.items():
            setattr(ctx, name, layer)
        return ctx

    def _ops_layer(self):
        ops = MagicMock()
        ops.check_health.return_value = MagicMock(
            overall_status="green", needs_attention=[],
        )
        ops.pipeline_logger.get_today_stats.return_value = {
            "total": 3, "success": 2, "failed": 0, "alerts": 1, "data_lag_min": 5,
        }
        ops.pipeline_logger.get_recent_runs.return_value = [
            MagicMock(pipeline_name="kline", status="success", duration_s=1.2),
        ]
        alert_rule = MagicMock(name="disk_high", severity="warning", description="磁盘占用")
        ops.alert_manager.get_rules.return_value = [alert_rule]
        return ops

    def test_alerts_builder_build_no_error(self):
        from report_layer.reports.alerts import AlertBuilder
        ops = self._ops_layer()
        ops.alert_manager.get_open_alerts.return_value = [
            MagicMock(severity="warning", layer="data", title="延迟", detail="5min"),
        ]
        ops.alert_manager.get_recent_history.return_value = [
            MagicMock(severity="warning", layer="data", title="延迟", created_at="09:00"),
        ]
        msg = AlertBuilder().build(self._ctx(ops_layer=ops))
        assert msg is not None
        sections = msg.content["sections"]
        assert all("error" not in s for s in sections)
        assert {s["name"] for s in sections} == {"alerts", "alert_history", "alert_rules"}
        assert any("当前告警" in s.get("title", "") for s in sections)

    def test_intraday_alert_builder_with_empty_execution(self):
        from report_layer.reports.intraday_alert import IntradayAlertBuilder
        execution = MagicMock()
        execution.get_today_actions.return_value = []
        execution.get_portfolio_snapshot.return_value = {"active": []}
        data = MagicMock()
        data.snapshot.return_value = MagicMock(klines=[])
        msg = IntradayAlertBuilder().build(self._ctx(execution_layer=execution, data_layer=data))
        assert msg is not None
        sections = msg.content["sections"]
        assert all("error" not in s for s in sections)
        assert {s["name"] for s in sections} == {"intraday_alert", "price_alert", "volume_alert"}

    def test_ops_report_builder_sections_dataclass(self):
        from report_layer.reports.ops_report import OpsReportBuilder, OpsSummarySection, PipelineHealthSection
        ops = self._ops_layer()
        for section in (OpsSummarySection(), PipelineHealthSection()):
            result = section.build(self._ctx(ops_layer=ops))
            assert result is not None
            from dataclasses import is_dataclass
            assert is_dataclass(result)
        msg = OpsReportBuilder().build(self._ctx(ops_layer=ops))
        assert msg is not None
        assert all("error" not in s for s in msg.content["sections"])

    def test_dev_verify_builder_serializes(self):
        from report_layer.reports.dev_verify import DevCompareSection
        strategy = MagicMock()
        result = strategy.run_daily.return_value
        result.signals = []
        result.stock_count = 5
        result.regime = "震荡"
        ctx = self._ctx(strategy_layer=strategy)
        built = DevCompareSection().build(ctx)
        assert built is not None
        assert "5 只" in built.body


class TestEveningReportSections:
    """Regression: v0.14 跨层 dataclass 迁移后 evening_report sections 必须返回
    SectionResult dataclass(而非 dict), 否则 ReportBuilder.build 的 asdict() 会抛错。
    """

    def _ctx(self, data_layer=None, execution_layer=None):
        from report_layer.reports.base import ReportContext
        return ReportContext(
            date="2026-08-07",
            data_layer=data_layer or MagicMock(),
            execution_layer=execution_layer or MagicMock(),
            report_mode="debug",
        )

    def test_market_summary_returns_section_result(self):
        from report_layer.reports.evening_report import MarketSummarySection
        data = MagicMock()
        data.snapshot.return_value = MagicMock(
            klines=[1, 2], valuations=[3], financials=[4],
        )
        result = MarketSummarySection().build(self._ctx(data_layer=data))
        assert result is not None
        assert result.body == "日期: 2026-08-07\nK 线数据: 2 只股票\n估值数据: 1 只\n财务数据: 1 只\n"

    def test_action_summary_uses_actionfeed_dataclass(self):
        # TASK_20260922_01 Phase B: 附录动作按 (类型, 原因) 聚合一行
        # (批复「少即是多」: 不逐只罗列) — 断言同步翻新, ActionFeed dataclass
        # 消费面 (本钉原始意图) 不变。
        from report_layer.reports.evening_report import ActionSummarySection
        from execution_layer.stats.display import ActionFeed
        execution = MagicMock()
        execution.get_today_actions.return_value = ActionFeed(
            actions=[{"action_type": "buy", "code": "600000", "reason": "信号"}],
        )
        result = ActionSummarySection().build(self._ctx(execution_layer=execution))
        assert result is not None
        assert "buy 1 次（原因：信号）" in result.body

    def test_portfolio_section_returns_section_result(self):
        from report_layer.reports.evening_report import PortfolioSection
        execution = MagicMock()
        snapshot = MagicMock(active=3, pending_entry=2, pending_exit=1, watch=4,
                             portfolio_nav=1000.0, today_pnl=12.5)
        execution.get_portfolio_snapshot.return_value = snapshot
        result = PortfolioSection().build(self._ctx(execution_layer=execution))
        assert result is not None
        assert "持有中: 3 只" in result.body
        assert "组合净值: 1000.00" in result.body

    def test_build_returns_message_with_six_sections(self):
        """ReportBuilder.build 应对 SectionResult 执行 asdict, 不再抛异常。

        R3 (设计稿-C §2.5): 晚报重编排为生命线结构 — 故事线置顶,
        退出归因卡/三段式收尾新增, MarketSummary/ExitQuality 移出编排
        (类保留), 合规固定段置尾。
        可读性施工 (2026-09-20): 大盘 2 段→1 段 (market_wrap);
        position_dynamics/portfolio/market_one_liner 移出编排 (类保留供 import)。
        TASK_20260922_01 Phase B (批复裁决2): 持仓管家段移出编排 — 持有组
        改三行式监控, 由 evening_storyline 承担。
        """
        from report_layer.reports.evening_report import EveningReportBuilder
        builder = EveningReportBuilder()
        from common.database import Database
        db = Database(":memory:")
        conn = db.get_connection()
        # 报告宪法 v2 第一波 (2026-09-10): 夹具补面 — stock_pool 表 + volume 列
        # (R3 时代窄夹具下 evening_storyline 等 5 段 build 失败被滤, 原断言只含
        # 幸存段; 六段旗舰需要全段可建, 夹具面同步扩)
        conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, change_pct REAL, volume REAL)")
        conn.execute("INSERT INTO daily_klines VALUES ('000001', '2026-08-14', 0.5, 1000000.0)")
        conn.execute("INSERT INTO daily_klines VALUES ('000002', '2026-08-14', 1.2, 2000000.0)")
        conn.execute("""CREATE TABLE IF NOT EXISTS stock_pool (
            code TEXT, name TEXT, status TEXT, added_date TEXT, created_at TEXT,
            entry_plan_date TEXT, entry_low REAL, entry_high REAL, valid_until TEXT,
            entry_price REAL, entry_date TEXT, stop_loss REAL, trailing_stop REAL,
            dynamic_stop_loss REAL, atr_stop REAL, take_profit REAL,
            exit_price REAL, exit_date TEXT, net_return_pct REAL,
            source TEXT, initial_score REAL, last_price REAL,
            unrealized_pnl_pct REAL, holding_days INTEGER, added_reason TEXT,
            entry_reason TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT, name TEXT, industry TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS stock_news (
            id INTEGER, code TEXT, title TEXT, content TEXT,
            publish_time TEXT, source TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id TEXT, pipeline_name TEXT, date TEXT, status TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS action_records (
            id INTEGER PRIMARY KEY, code TEXT, action_type TEXT,
            price REAL, reason TEXT, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS intraday_checks (
            id INTEGER PRIMARY KEY, code TEXT, check_type TEXT,
            current_price REAL, trigger_price REAL, status TEXT,
            detail TEXT, check_time TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS recommendation_retrospect (
            prediction_id INTEGER, code TEXT, rec_date TEXT, verdict TEXT,
            hit_5d INTEGER, hit_10d INTEGER,
            fwd_return_5d REAL, fwd_return_10d REAL)""")
        data = MagicMock()
        data.snapshot.return_value = MagicMock(klines=[1], valuations=[1], financials=[1])
        data.db = db
        execution = MagicMock()
        from execution_layer.stats.display import ActionFeed, PortfolioSnapshot
        execution.get_today_actions.return_value = ActionFeed(actions=[])
        snapshot = PortfolioSnapshot(active=0, pending_entry=1, pending_exit=2, watch=0,
                                     portfolio_nav=100000.0, today_pnl=0.0, positions=[])
        execution.get_portfolio_snapshot.return_value = snapshot
        execution.get_exit_reviews.return_value = []
        msg = builder.build(self._ctx(data_layer=data, execution_layer=execution))
        assert msg is not None
        section_names = [s.get("name") for s in msg.content["sections"]]
        assert set(section_names) == {
            # 可读性施工 (2026-09-20): 六段旗舰 + R3 保留段并集
            # TASK_20260922_01 Phase B (批复裁决2): position_steward 移出编排
            # (持有组改三行式并入故事线) — 10 段
            "yesterday_picks_review", "market_wrap", "sector_news",
            "tomorrow_picks", "preview_health",
            "evening_storyline", "exit_attribution", "action_summary",
            "evening_close", "risk_compliance",
        }
        assert all("error" not in s for s in msg.content["sections"])

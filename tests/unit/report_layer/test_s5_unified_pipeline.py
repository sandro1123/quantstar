"""F5.1 报告统一管线测试 — morning/midday builder 全 section + 新 section 复用 data_layer

迁移点:
1. MorningBriefBuilder 补齐 NewsSection (今日要闻) + IndicesSection (大盘指数)
2. IndicesSection / MiddayMarketSection 复用 fetch_realtime_indices (gtimg)
3. NewsSection 复用 data_layer NewsCollector
4. MiddayMarketSection 死代码 (snapshot.indices 不存在) → 改走 gtimg 实时指数

R1 (2026-08-28): 晨报重编排为七段行动指引结构 (设计稿-C §2.2)。
原 11 段断言更新: overnight/news/indices 并入 market_background 压缩段,
watch_focus/stock_picks/ic_weight/agent_trade_plan/market_overview 移出晨报
(旧模块保留, 仅改编排)。
"""
from unittest.mock import MagicMock, patch

from report_layer.reports.base import ReportContext
from report_layer.reports.midday_brief import MiddayBriefBuilder
from report_layer.reports.morning_brief import MorningBriefBuilder
from report_layer.sections.indices import IndicesSection, fetch_realtime_indices
from report_layer.sections.midday_market import MiddayMarketSection
from report_layer.sections.news import NewsSection

class TestBuilderComposition:
    """builder 构建出全 section (R1: 七段行动指引结构)"""

    def test_morning_brief_has_all_sections(self):
        names = [s.name for s in MorningBriefBuilder().sections()]
        assert names == [
            "outbox_watermark",      # P0-2 ① 投递水位, 置顶, 无滞留自动隐身
            "action_candidates",     # R1: 🎯 今日行动候选 (行动卡组)
            "avoid_cards",           # R1: 🔴 回避卡
            "advisory_morning",      # 🧭 晨间投顾 (卡VII, 隔夜+昨收视角)
            "yesterday_followup",    # R1: 🧵 昨日候选, 现在如何
            "market_background",     # R1: 📊 市场背景 (外盘+指数+要闻压缩)
            "risk_compliance",       # R1: ⚠️ 风险提示 + 合规固定段
            "conclusion",            # 结论
        ]

    def test_morning_brief_background_order(self):
        # 背景压缩段内部顺序: 外盘 → 指数 → 要闻 (与旧脚本一致)
        names = [s.name for s in MorningBriefBuilder().sections()]
        assert names.index("market_background") > names.index("yesterday_followup")
        assert names.index("risk_compliance") > names.index("market_background")

    def test_midday_brief_has_all_sections(self):
        # R3 (设计稿-C §2.4): 候选跟踪置顶, 涨跌停池降附录置尾 (D4)
        names = [s.name for s in MiddayBriefBuilder().sections()]
        assert names == [
            "midday_candidate_tracking", "advisory_midday", "midday_market",
            "midday_sectors", "midday_news", "midday_strategy", "midday_alerts",
        ]

    def test_morning_brief_builds_message(self):
        # 全 mock context: 各 section 走降级/空数据路径, 但都应产出段落 (不抛错)
        from report_layer.sections.overnight_market import OvernightMarketSection
        ctx = ReportContext(date="2026-08-17")
        with patch("report_layer.sections.indices.fetch_realtime_indices", return_value=[]), \
             patch.object(NewsSection, "_get_items", return_value=[]), \
             patch.object(OvernightMarketSection, "_fetch_overnight", return_value={}):
            msg = MorningBriefBuilder().build(ctx)
        assert msg is not None
        titles = {s.get("title") for s in msg.content["sections"]}
        assert "📊 市场背景" in titles
        # 合规固定段每报必带 (范式 8)
        bodies = "\n".join(s.get("body", "") for s in msg.content["sections"])
        assert "非投资建议" in bodies

class TestIndicesSection:
    def test_renders_gtimg_indices(self):
        fake = [
            {"name": "上证", "price": 3250.12, "change_pct": 1.23},
            {"name": "深证", "price": 10500.5, "change_pct": -0.85},
        ]
        with patch("report_layer.sections.indices.fetch_realtime_indices", return_value=fake):
            result = IndicesSection().build(ReportContext(date="2026-08-17"))
        assert "上证" in result.body
        assert "🔴" in result.body  # 涨 → 红 (A股惯例)
        assert "🟢" in result.body  # 跌 → 绿
        assert result.sources == ["gtimg"]

    def test_empty_indices_graceful(self):
        with patch("report_layer.sections.indices.fetch_realtime_indices", return_value=[]):
            result = IndicesSection().build(ReportContext(date="2026-08-17"))
        assert "暂不可用" in result.body
        assert result.sources == ["gtimg"]

    def test_fetch_realtime_indices_returns_list(self):
        # 真实函数签名: 失败/超时返回 list (不抛异常)
        with patch("data_layer.sources.gtimg_source.GTimgSourceAdapter.get_data",
                   return_value=[{"name": "上证", "price": 1.0, "change_pct": 0.0}]):
            out = fetch_realtime_indices()
        assert isinstance(out, list)

class TestNewsSection:
    def test_renders_news_with_sentiment_tag(self):
        items = [
            {"title": "央行降准", "summary": "释放流动性", "sentiment": "bullish"},
            {"title": "某公司减持", "summary": "股东减持公告", "sentiment": "bearish"},
        ]
        with patch.object(NewsSection, "_get_items", return_value=items):
            result = NewsSection().build(ReportContext(date="2026-08-17"))
        assert "央行降准" in result.body
        assert "[利好]" in result.body
        assert "[利空]" in result.body
        assert result.sources == ["news_collector"]

    def test_empty_news_graceful(self):
        with patch.object(NewsSection, "_get_items", return_value=[]):
            result = NewsSection().build(ReportContext(date="2026-08-17"))
        assert "暂无重要新闻" in result.body

    def test_clean_summary_strips_title_prefix(self):
        s = NewsSection._clean_summary("【央行降准】具体内容如下", "央行降准")
        assert s == "具体内容如下"

class TestMiddayMarketIndices:
    """F5.1: snapshot.indices 死代码 → 改走 gtimg 实时指数"""

    def test_uses_realtime_indices_not_snapshot(self):
        fake = [{"name": "上证", "price": 3250.0, "change_pct": 0.5}]
        # data_layer snapshot 无 indices 字段 (真实 DataSnapshot 契约)
        dl = MagicMock()
        dl.snapshot.return_value = MagicMock(spec=[])  # 无 indices/market 属性
        ctx = ReportContext(date="2026-08-17", data_layer=dl)
        with patch("report_layer.sections.indices.fetch_realtime_indices", return_value=fake):
            result = MiddayMarketSection().build(ctx)
        assert "上证" in result.body
        assert result.sources == ["gtimg"]

    def test_no_data_graceful(self):
        dl = MagicMock()
        dl.snapshot.return_value = MagicMock(spec=[])
        ctx = ReportContext(date="2026-08-17", data_layer=dl)
        with patch("report_layer.sections.indices.fetch_realtime_indices", return_value=[]):
            result = MiddayMarketSection().build(ctx)
        assert "暂不可用" in result.body

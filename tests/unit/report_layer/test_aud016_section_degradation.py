"""AUD-016 回归: 报告段降级必须可观测。

AST 实测 (2026-09-14): src/report_layer/sections/ 共 40 个段文件, 26 个含
`except -> 返回空` 面, 41 个处理点, **其中 23 个连一行日志都没有**。
编排器 reports/base.py 本身有兜底 (段抛异常 -> warning + 落 error 条目),
但段在内部自己吞掉后返回 None, 兜底就永远看不到 —— 于是:
  段取数炸了 -> 渲染成空段 -> 报告整体仍报 success -> 运维与读者双盲。
这是「morning_brief 0 行 = 结构性」这类症状的结构解。

契约: 段降级时调 note_degraded() 留痕; 编排器汇总进 content["degraded_sections"],
并给该段条目打 degraded 位 —— 「真没内容」(return None 不留痕) 与「取数炸了」
(留痕) 从此可分辨。
"""
from report_layer.reports.base import ReportBuilder, ReportContext
from report_layer.sections.base import Section, SectionResult, note_degraded


class _OkSection(Section):
    @property
    def name(self):
        return "ok_section"

    def build(self, context):
        return SectionResult(name=self.name, title="正常段", body="有内容")


class _SilentlyDegradedSection(Section):
    """取数炸了但自己吞掉 —— 生产里 23 个零日志处理点的形态。"""

    @property
    def name(self):
        return "degraded_section"

    def build(self, context):
        try:
            raise RuntimeError("数据源查询失败")
        except Exception as e:
            note_degraded(self.name, e, "行情表查询失败")
            return None


class _LegitimatelyEmptySection(Section):
    """今天真的没内容 (如无告警) —— 不是降级, 不得计入。"""

    @property
    def name(self):
        return "empty_section"

    def build(self, context):
        return None


class _RaisingSection(Section):
    @property
    def name(self):
        return "raising_section"

    def build(self, context):
        raise ValueError("炸穿到编排器")


def _builder(sections):
    class _B(ReportBuilder):
        @property
        def content_type(self):
            return "test_report"

        @property
        def title(self):
            return "测试报告"

        def sections(self):
            return sections

    return _B()


def _build(sections):
    return _builder(sections).build(ReportContext(date="2026-09-14"))


class TestDegradationIsVisible:
    def test_silent_degradation_is_counted_and_named(self):
        msg = _build([_OkSection(), _SilentlyDegradedSection()])
        assert msg is not None
        content = msg.content
        assert content.get("degraded_sections") == 1, (
            "段内自吞的降级必须被编排器汇总, 否则报告整体仍报成功 = 运维读者双盲")
        names = [s.get("name") for s in content["sections"] if s.get("degraded")]
        assert "degraded_section" in names, "降级段条目须带 degraded 位可定位"

    def test_legitimately_empty_is_not_degraded(self):
        msg = _build([_OkSection(), _LegitimatelyEmptySection()])
        assert msg is not None
        assert msg.content.get("degraded_sections", 0) == 0, (
            "真没内容不是降级 —— 两者必须可分辨, 否则计数没有意义")

    def test_raising_section_still_recorded(self):
        """既有兜底不得破: 抛穿的段照旧落 error 条目。"""
        msg = _build([_OkSection(), _RaisingSection()])
        assert msg is not None
        errs = [s for s in msg.content["sections"] if s.get("error")]
        assert len(errs) == 1
        assert errs[0]["name"] == "raising_section"


class TestLedgerIsolation:
    def test_ledger_does_not_leak_across_reports(self):
        """一次报告的降级不得计入下一次 (跨报告污染会让计数恒增)。"""
        _build([_SilentlyDegradedSection()])
        msg2 = _build([_OkSection()])
        assert msg2.content.get("degraded_sections", 0) == 0, "降级账须按报告隔离"

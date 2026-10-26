"""F5.4 SectionResult.sources + formatter 渲染测试

可读性施工 (2026-09-20): 飞书卡片正文不再渲染「来源:」尾注 (内部表名不进
用户视野); sources 仍保留在 SectionResult/Message content 供归档与
TextFormatter 使用。
"""
from delivery_layer.message import Message
from report_layer.formatters.feishu_card import FeishuCardFormatter
from report_layer.formatters.text import TextFormatter
from report_layer.sections.base import SectionResult


class TestSectionResultSources:
    def test_default_empty(self):
        r = SectionResult(title="t", body="b")
        assert r.sources == []

    def test_sources_independent_per_instance(self):
        a = SectionResult()
        b = SectionResult()
        a.sources.append("x")
        assert b.sources == []  # field(default_factory) 不共享


class TestFeishuCardSources:
    def _msg(self, sections):
        return Message(content={"sections": sections, "date": "2026-08-17"},
                       content_type="test", title="测试报告", severity="info")

    def test_sources_not_rendered_in_card_body(self):
        # 可读性施工: 「来源: stock_pool · pipeline_runs」尾注剥离出卡片正文
        msg = self._msg([{"title": "段A", "body": "内容",
                          "sources": ["LLM:sensenova", "rule-based-fallback"]}])
        card = FeishuCardFormatter().format(msg)
        contents = [e["text"]["content"] for e in card["card"]["elements"]
                    if e.get("tag") == "div"]
        assert not any("来源" in c for c in contents)

    def test_empty_sources_not_rendered(self):
        msg = self._msg([{"title": "段A", "body": "内容", "sources": []}])
        card = FeishuCardFormatter().format(msg)
        contents = [e["text"]["content"] for e in card["card"]["elements"]
                    if e.get("tag") == "div"]
        assert not any("来源" in c for c in contents)

    def test_missing_sources_key_backward_compat(self):
        # 旧 section dict 无 sources 键不崩
        msg = self._msg([{"title": "段A", "body": "内容"}])
        card = FeishuCardFormatter().format(msg)
        assert card["card"]["elements"]


class TestTextFormatterSources:
    def test_sources_line_appended(self):
        msg = Message(content={"sections": [
            {"title": "段A", "body": "内容", "sources": ["gtimg"]}],
            "date": "2026-08-17"},
            content_type="test", title="测试", severity="info")
        out = TextFormatter().format(msg)
        assert "[来源: gtimg]" in out

    def test_no_sources_no_line(self):
        msg = Message(content={"sections": [{"title": "段A", "body": "内容"}],
                               "date": "2026-08-17"},
                      content_type="test", title="测试", severity="info")
        out = TextFormatter().format(msg)
        assert "来源" not in out

"""ReportBuilder 抽象基类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from delivery_layer.message import Message


@dataclass
class ReportContext:
    date: str
    data_layer: Any = None
    strategy_layer: Any = None
    execution_layer: Any = None
    agent_layer: Any = None
    backtest_layer: Any = None
    ops_layer: Any = None
    llm_service: Any = None
    report_mode: str = "debug"
    _cache: dict = field(default_factory=dict)

    def cached(self, key: str, factory):
        if key not in self._cache:
            self._cache[key] = factory()
        return self._cache[key]

    @property
    def is_preview(self) -> bool:
        return self.report_mode == "preview"


class ReportBuilder(ABC):
    @property
    @abstractmethod
    def content_type(self) -> str:
        ...

    @property
    def title(self) -> str:
        return ""

    @property
    def severity(self) -> str:
        return "info"

    @abstractmethod
    def sections(self) -> list:
        ...

    def build(self, context: ReportContext) -> Message | None:
        from dataclasses import asdict

        from common.logging import get_logger

        from report_layer.sections.base import drain_degraded

        logger = get_logger("report.builder")
        # AUD-016: 报告开头先清账 —— 降级计数按报告隔离, 否则跨报告串账恒增。
        drain_degraded()
        section_data = []
        degraded_total = 0
        for section in self.sections():
            try:
                data = section.build(context)
                notes = drain_degraded()          # 本段留下的降级痕 (段内自吞面)
                if data is not None:
                    entry = asdict(data)
                    if notes:
                        entry["degraded"] = True
                        entry["degrade_reason"] = "; ".join(
                            n["reason"] for n in notes)[:300]
                    section_data.append(entry)
                elif notes:
                    # 段自吞后返回 None: 旧实现在此彻底消失 (读者与运维都看不到
                    # 「本段本应有内容但取数炸了」), 现在留一条可见条目。
                    section_data.append({
                        "name": section.name, "degraded": True,
                        "degrade_reason": "; ".join(
                            n["reason"] for n in notes)[:300],
                    })
                if notes:
                    degraded_total += 1
            except Exception as e:
                drain_degraded()                  # 抛穿面不重复计, 但须清账
                logger.warning(f"段落 {section.name} 生成失败: {e}")
                section_data.append({"name": section.name, "error": str(e)})
        if not section_data:
            return None
        content = {"sections": section_data, "date": context.date}
        if degraded_total:
            # 只在非零时写键 —— 正常报告的 content 形状不变 (既有消费面零影响)
            content["degraded_sections"] = degraded_total
            logger.warning(
                f"报告 {self.content_type}: {degraded_total} 段降级 "
                f"(段内取数失败但已留痕, 非「今日无内容」)")
        return Message(
            content=content,
            content_type=self.content_type,
            title=self.title,
            severity=self.severity,
        )

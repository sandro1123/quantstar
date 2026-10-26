"""Section 抽象基类"""

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from common.logging import get_logger

logger = get_logger("report.section")

# AUD-016: 段降级留痕账 (线程本地 — 管道用线程池跑, 不同报告不得串账)。
_ledger = threading.local()


def _bucket() -> list:
    b = getattr(_ledger, "items", None)
    if b is None:
        b = []
        _ledger.items = b
    return b


def note_degraded(section_name: str, exc: BaseException | None = None,
                  detail: str = "") -> None:
    """AUD-016: 段内吞掉异常时的**统一留痕点**。

    病根: 段自己 except 后返回空, 编排器 (reports/base.py) 的兜底就永远看不到 ——
    于是「段取数炸了」与「今天真的没内容」在报告里长得一模一样, 报告整体照报 success,
    运维与读者双盲。AST 实测: 40 个段文件里 26 个有这个面, 41 个处理点,
    其中 23 个连一行日志都没有。这是「morning_brief 0 行 = 结构性」的结构解。

    调用本函数 = 写 warning + 记本轮降级账; 编排器汇总后给该段条目打 degraded 位。
    **不改变控制流** —— 段原本返回什么还返回什么, 故加这行调用零风险。
    """
    reason = (detail or (str(exc) if exc is not None else "")).strip()
    suffix = f" ({type(exc).__name__})" if exc is not None else ""
    logger.warning(f"段落 {section_name} 降级: {reason}{suffix}")
    _bucket().append({"name": section_name, "reason": reason[:200]})


def drain_degraded() -> list:
    """取出并清空本线程当前的降级账 (编排器按段调用, 报告开头先清一次)。"""
    b = _bucket()
    out = list(b)
    b.clear()
    return out


@dataclass
class SectionResult:
    title: str = ""
    body: str = ""
    subtitle: str = ""
    name: str = ""
    # F5.4: 数据来源标注 (如 ["LLM:sensenova", "rule-based-fallback"]), 空=不渲染
    sources: list = field(default_factory=list)


class Section(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def build(self, context) -> SectionResult | None:
        ...

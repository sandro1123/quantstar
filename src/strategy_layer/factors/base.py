"""因子基类 — 定义 FactorContext + BaseFactor"""
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


def is_finite_number(value) -> bool:
    """值是否为有限数值（None/NaN/Inf/bool/非数值 → False）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_valid_positive(value) -> bool:
    """值是否为有限正数（PE/PB/PS/market_cap 等有效域 > 0）。"""
    return is_finite_number(value) and value > 0


def is_valid_nonzero(value) -> bool:
    """值是否为有限非零数（除数保护）。"""
    return is_finite_number(value) and value != 0


@dataclass
class FactorContext:
    """因子计算上下文 — 由 FactorEngine 预取"""
    code: str
    date: str
    klines: list = field(default_factory=list)      # list[dict] 历史 K 线，按日期升序
    financials: list = field(default_factory=list)   # list[dict] 财务数据
    valuations: list = field(default_factory=list)   # list[dict] 估值数据
    moneyflow: list = field(default_factory=list)    # list[dict] 资金流数据
    concept_data: list = field(default_factory=list) # list[dict] 概念板块数据
    lhb_data: list = field(default_factory=list)     # list[dict] 龙虎榜数据
    analyst_data: list = field(default_factory=list) # list[dict] 分析师预测数据
    sentiment_data: dict = field(default_factory=dict)  # dict 市场情绪数据
    shortterm_data: dict = field(default_factory=dict)  # dict ST-STYLE 市场级盘口数据 (ladder/auction)
    sector: str = ""                                 # 行业分类


@dataclass
class FactorResult:
    code: str
    date: str
    factor_name: str
    # #122 案A (统筹 GO sm-bc61961126d0435b): 默认 0.0→None — bare 守卫
    # return (191 处) 吃默认后 0.0 经 zscore 变非零假 z 注入截面 (BJ 920189
    # roe_quality=-1.03 实证); None 被 engine._clean_impl:285 valid 过滤器
    # 单点拦下, 不入清洗矩阵。显式 score=0.0 (形态B, 15 处白名单) 语义不变。
    score: float | None = None
    raw_value: float | None = None
    details: dict = field(default_factory=dict)
    # 卡3 D' 面 (readiness-announce-aware-unify, 2026-10-03, 判例 #163):
    # 因子的真源数据期号 — 「真源最近一次更新是什么时候」的显形回答。
    # 默认 None = 「答不出」可见态 (假活判定锚: 答不出的因子不进 Beta 推荐)。
    source_period: str | None = None


class BaseFactor(ABC):
    name: str = ""
    description: str = ""
    category: str = ""  # "momentum" | "value" | "quality" | "technical" | "sentiment"
    window_size: int = 1  # 需要的历史交易日窗口
    # ST-05 方案A (决策账本 §十三): compute_only = 照常计算并全量入库 factor_daily
    # (供 IC 评估积累证据), 但不参与 composite_score 聚合。默认 False;
    # 不在聚合权重表 DEFAULT_WEIGHTS 内的活跃因子, 由注册表级函数
    # factors.compute_only_factor_names() 同步标记, 无需逐个因子类修改。
    compute_only: bool = False

    @abstractmethod
    def compute(self, ctx: FactorContext) -> FactorResult:
        ...

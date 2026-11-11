# ruff: noqa: F401
"""因子注册 — 所有可用因子 (125 基础 + 20 Alpha = 145)"""
from strategy_layer.factors.accrual import AccrualFactor
from strategy_layer.factors.alpha_factors import Alpha48Factor
from strategy_layer.factors.concept import ConceptCountFactor, ConceptHeatFactor
from strategy_layer.factors.dividend import DividendFactor
from strategy_layer.factors.extended_liquidity import (
    AmihudIlliquidityFactor,
    HighLowSpreadFactor,
    LiquidityMomentumFactor,
    TurnoverRateFactor,
    VolumeTrendFactor,
)
from strategy_layer.factors.extended_momentum import (
    Momentum60Factor,
    Momentum120Factor,
    Momentum250Factor,
    PriceAccelerationFactor,
    RelativeStrengthFactor,
)
from strategy_layer.factors.extended_quality import (
    CurrentRatioFactor,
    DebtRatioFactor,
    EarningsStabilityFactor,
    OperatingMarginFactor,
    ROAFactor,
)
from strategy_layer.factors.extended_technical import (
    ADXFactor,
    CCIFactor,
    OBVFactor,
    StochasticKFactor,
    WilliamsRFactor,
)

# 扩展因子 (25)
from strategy_layer.factors.extended_value import (
    BPFactor,
    CFPFactor,
    DividendYieldFactor,
    EPFactor,
    SPFactor,
)
from strategy_layer.factors.extended_volatility import (
    DownsideVolFactor,
    MaxDrawdownFactor,
    RealizedVol20Factor,
    RealizedVol60Factor,
    VolOfVolFactor,
)
from strategy_layer.factors.growth import GrowthFactor
from strategy_layer.factors.industry_momentum import IndustryMomentumFactor
from strategy_layer.factors.kbar import (
    KlenFactor,
    Klow2Factor,
    KlowFactor,
    Kmid2Factor,
    KmidFactor,
    Ksft2Factor,
    KsftFactor,
    Kup2Factor,
    KupFactor,
)
from strategy_layer.factors.leverage import LeverageFactor
from strategy_layer.factors.lhb_analyst import (
    AnalystConsensusFactor,
    LhbNetBuyFactor,
)
# LhbDramaScoreFactor — ⚫ 弃用, 不再导入/注册 (统筹 sm-a5a07066c2944051 裁定:
# lhb_daily.drama_score 上游源 zzshare lhb 根本没有 drama 字段, 写入方从不写,
# 全列 NULL; 类保留在 lhb_analyst.py 不删, 派生异动因子方案入 Q-50 排队)。
from strategy_layer.factors.liquidity import LiquidityFactor
from strategy_layer.factors.ma_dev import Ma5DevFactor, Ma10DevFactor
from strategy_layer.factors.market_sentiment import MarketSentimentFactor
from strategy_layer.factors.momentum import (
    MomentumFactor,
    Roc5Factor,
    Roc10Factor,
    Roc20Factor,
    Roc60Factor,
)
from strategy_layer.factors.moneyflow import MoneyflowMainNetFactor
from strategy_layer.factors.pattern import PatternFactor
from strategy_layer.factors.price_extreme import Max5Factor, Min5Factor, Rank5Factor, Rank10Factor
from strategy_layer.factors.price_volume import Cord5Factor, Cord20Factor, Corr5Factor, Corr10Factor
from strategy_layer.factors.quality import QualityFactor
from strategy_layer.factors.seasonality import SeasonalityFactor
from strategy_layer.factors.sentiment import (
    SentimentFactor,
    Vma5Factor,
    Vma10Factor,
    Vma20Factor,
    Vstd5Factor,
    Vstd10Factor,
)
from strategy_layer.factors.short_term_momentum import ShortTermMomentumFactor
from strategy_layer.factors.size import SizeFactor
from strategy_layer.factors.shortterm import (
    AuctionStrengthFactor,
    LimitUpDnaFactor,
    MarketEmotionProxyFactor,
    OvernightGapFactor,
)
from strategy_layer.factors.technical import BbWidthFactor, MacdHistFactor, Rsi14Factor
from strategy_layer.factors.updown_count import (
    Cntd5Factor,
    Cntd10Factor,
    Cntd20Factor,
    Cntp5Factor,
    Cntp10Factor,
    Cntp20Factor,
)
from strategy_layer.factors.updown_volume import (
    Sumd5Factor,
    Sumd10Factor,
    Sumd20Factor,
    Sump5Factor,
    Sump10Factor,
    Sump20Factor,
)
from strategy_layer.factors.value import ValueFactor
from strategy_layer.factors.volatility import (
    Std5Factor,
    Std10Factor,
    Std20Factor,
    VolatilityFactor,
)

# 扩展因子 v2 (26) — 暂时禁用，需修复因子bug
# from strategy_layer.factors.extended_value2 import (
#     FcfEvFactor, EvEbitdaFactor, PegFactor, EarningsYieldFactor,
#     BookYieldFactor, RevenueYieldFactor, CashFlowYieldFactor,
#     MarketCapFactor, EnterpriseValueFactor, TotalReturnFactor,
# )
# from strategy_layer.factors.extended_momentum2 import (
#     Momentum480Factor, MomentumReversalFactor, SeasonalMomentumFactor,
#     CrossSectionalMomentumFactor, IndustryMomentumFactor,
#     MomentumTrendFactor, MomentumStrengthFactor, MomentumConsistencyFactor,
# )
# from strategy_layer.factors.extended_quality2 import (
#     AssetTurnoverFactor, NetProfitMarginFactor, RevenueGrowthStabilityFactor,
#     OperatingCashFlowRatioFactor, LeverageFactor, QuickRatioHealthFactor,
#     EarningsPerShareFactor, BookValuePerShareFactor,
# )

ALL_FACTORS = [
    # 原始 16 复合因子
    MomentumFactor, ValueFactor, QualityFactor, VolatilityFactor,
    SentimentFactor, SizeFactor, LiquidityFactor, GrowthFactor,
    LeverageFactor, ShortTermMomentumFactor, SeasonalityFactor,
    AccrualFactor, DividendFactor, IndustryMomentumFactor,
    PatternFactor, Alpha48Factor,
    # KBar 形态 (9)
    KmidFactor, KlenFactor, Kmid2Factor, KupFactor, Kup2Factor,
    KlowFactor, Klow2Factor, KsftFactor, Ksft2Factor,
    # 滚动收益率 (4)
    Roc5Factor, Roc10Factor, Roc20Factor, Roc60Factor,
    # 均线偏离 (2)
    Ma5DevFactor, Ma10DevFactor,
    # 波动率 (3)
    Std5Factor, Std10Factor, Std20Factor,
    # 价格极值 (4)
    Max5Factor, Min5Factor, Rank5Factor, Rank10Factor,
    # 量价相关性 (4)
    Corr5Factor, Corr10Factor, Cord5Factor, Cord20Factor,
    # 涨跌计数 (6)
    Cntp5Factor, Cntp10Factor, Cntp20Factor, Cntd5Factor, Cntd10Factor, Cntd20Factor,
    # 涨跌量 (6)
    Sump5Factor, Sump10Factor, Sump20Factor, Sumd5Factor, Sumd10Factor, Sumd20Factor,
    # 成交量 (5)
    Vma5Factor, Vma10Factor, Vma20Factor, Vstd5Factor, Vstd10Factor,
    # 技术指标 (3)
    Rsi14Factor, MacdHistFactor, BbWidthFactor,
    # 概念板块 (2)
    ConceptCountFactor, ConceptHeatFactor,
    # 龙虎榜/分析师 (2)
    LhbNetBuyFactor, AnalystConsensusFactor,
    # LhbDramaScoreFactor — ⚫ 弃用 (drama_score 无源可采, 见 DEPRECATED_LHB_COLUMNS)
    # 资金流 (1)
    MoneyflowMainNetFactor,
    # 市场情绪 (1)
    MarketSentimentFactor,
    # ST-STYLE 盘口四族 (统筹 sm-25d60e49f5e046d3; compute_only 上岗)
    LimitUpDnaFactor, AuctionStrengthFactor, OvernightGapFactor, MarketEmotionProxyFactor,
    # 扩展价值 (5) — 暂时禁用，需修复因子bug
    # EPFactor, BPFactor, SPFactor, CFPFactor, DividendYieldFactor,
    # 扩展动量 (5) — 暂时禁用
    # Momentum60Factor, Momentum120Factor, Momentum250Factor,
    # RelativeStrengthFactor, PriceAccelerationFactor,
    # 扩展质量 (5) — 暂时禁用
    # ROAFactor, OperatingMarginFactor, EarningsStabilityFactor,
    # DebtRatioFactor, CurrentRatioFactor,
    # 扩展波动率 (5) — 暂时禁用
    # RealizedVol20Factor, RealizedVol60Factor, DownsideVolFactor,
    # VolOfVolFactor, MaxDrawdownFactor,
    # 扩展流动性 (5) — 暂时禁用
    # AmihudIlliquidityFactor, TurnoverRateFactor, VolumeTrendFactor,
    # LiquidityMomentumFactor, HighLowSpreadFactor,
    # 扩展技术 (5) — 暂时禁用
    # StochasticKFactor, CCIFactor, ADXFactor, WilliamsRFactor, OBVFactor,
    # 扩展价值 v2 (10) — 暂时禁用
    # FcfEvFactor, EvEbitdaFactor, PegFactor, EarningsYieldFactor,
    # BookYieldFactor, RevenueYieldFactor, CashFlowYieldFactor,
    # MarketCapFactor, EnterpriseValueFactor, TotalReturnFactor,
    # 扩展动量 v2 (8) — 暂时禁用，需修复因子bug
    # Momentum480Factor, MomentumReversalFactor, SeasonalMomentumFactor,
    # CrossSectionalMomentumFactor, IndustryMomentumFactor,
    # MomentumTrendFactor, MomentumStrengthFactor, MomentumConsistencyFactor,
    # 扩展质量 v2 (8) — 暂时禁用，需修复因子bug
    # AssetTurnoverFactor, NetProfitMarginFactor, RevenueGrowthStabilityFactor,
    # OperatingCashFlowRatioFactor, LeverageFactor, QuickRatioHealthFactor,
    # EarningsPerShareFactor, BookValuePerShareFactor,
]

# 注册 Alpha 表达式因子
try:
    from strategy_layer.expression.factor_expression import register_all_alpha_expressions
    ALPHA_FACTORS = register_all_alpha_expressions()
    ALL_FACTORS.extend(ALPHA_FACTORS)
except Exception as e:
    import logging
    logging.getLogger("strategy.factors").warning(f"Alpha表达式注册失败: {e}")
    ALPHA_FACTORS = []


# ---- ST-05 方案A: compute_only 注册表级标记 (决策账本 §十三, 用户终审 2026-08-20) ----
# 契约:
#   * DEFAULT_WEIGHTS (aggregation/aggregator.py, 20 因子) 是唯一聚合权重基准 (ST-01 裁定);
#   * 不在权重表内的活跃因子为 compute_only: 照常计算、全量入库 factor_daily,
#     供 IC 观测积累证据, 但不参与 composite_score 聚合;
#   * 聚合器按权重表成员资格显式过滤并记录跳过计数, 不再依赖 weights.get(..., 0) 隐式默认;
#   * 因子不删只分批上岗: 未来 IC 调权 (record-only ≥20 交易日 → 周频+人在回路审批,
#     另行立项) 使因子进入权重表后, 该因子自动不再是 compute_only。
# 注册表按 ALL_FACTORS vs DEFAULT_WEIGHTS 惰性推导 (延迟导入避免循环依赖),
# 不在模块导入期展开; 调用下列函数时同步 BaseFactor.compute_only 类属性 (幂等全量同步)。


def compute_only_factor_names() -> frozenset[str]:
    """计算当前 compute_only 活跃因子名集合并同步类属性标记。

    Returns:
        frozenset[str]: ALL_FACTORS 活跃因子名 − DEFAULT_WEIGHTS 权重表名。
    """
    # 延迟导入: factors → aggregation → engine → factors 会成环, 仅运行期展开
    from strategy_layer.aggregation.aggregator import DEFAULT_WEIGHTS
    weighted = set(DEFAULT_WEIGHTS)
    names = frozenset(
        cls.name for cls in ALL_FACTORS
        if getattr(cls, "name", "") and cls.name not in weighted
    )
    for cls in ALL_FACTORS:  # 注册表级同步标记 (幂等), 避免逐个修改因子类
        cls.compute_only = getattr(cls, "name", "") in names
    return names


def is_compute_only(factor_name: str) -> bool:
    """因子名是否为 compute_only (活跃但不在聚合权重表内)。"""
    return factor_name in compute_only_factor_names()


# ---- 骨架期辅助 API (origin/main fc88fb70 引入, 保留以兼容调用方) ----
def get_factor_names() -> list[str]:
    return [f.name for f in ALL_FACTORS]


def get_factor_class(name: str):
    for f in ALL_FACTORS:
        if f.name == name:
            return f
    return None


# 注意: DEFAULT_WEIGHTS 的单一事实源是
# strategy_layer.aggregation.aggregator.DEFAULT_WEIGHTS (20 因子聚合基准),
# 此处不再重复定义, 避免双定义点。
__all__ = [
    "ALL_FACTORS", "ALPHA_FACTORS",
    "get_factor_names", "get_factor_class", "compute_only_factor_names",
]

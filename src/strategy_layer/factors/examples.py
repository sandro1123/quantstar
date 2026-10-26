# -*- coding: utf-8 -*-
"""示例因子 —— 演示如何实现一个因子。

QuantStar 开源骨架只提供 **因子框架**，不含任何具体策略因子实现。
使用者应在此自行实现因子（继承 BaseFactor，实现 compute()）。

本文件仅提供两个最小示例，用于跑通管线与说明接口，
**不构成任何投资建议，也不代表任何可用策略**。
"""
from __future__ import annotations

import math

from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


def _close(k) -> float:
    """K 线可能是 dict 或对象，统一取 close。"""
    if isinstance(k, dict):
        return float(k.get("close") or 0)
    return float(getattr(k, "close", 0) or 0)


class MomentumFactor20(BaseFactor):
    """示例因子：20 日动量 = (今收 - 20日前收) / 20日前收。"""

    name = "mom20"
    description = "20日动量（示例因子，非策略）"
    category = "momentum"
    window_size = 21

    def compute(self, ctx: FactorContext) -> FactorResult:
        kl = ctx.klines or []
        base = dict(code=ctx.code, date=ctx.date, factor_name=self.name)
        if len(kl) < self.window_size:
            return FactorResult(**base, score=None, details={"skip": "K线不足"})
        today, past = _close(kl[-1]), _close(kl[-self.window_size])
        if past <= 0 or today <= 0:
            return FactorResult(**base, score=None, details={"skip": "价格无效"})
        v = (today - past) / past
        return FactorResult(**base, score=round(v, 6), raw_value=round(v, 6))


class VolatilityFactor20(BaseFactor):
    """示例因子：20 日收益率标准差。"""

    name = "vol20"
    description = "20日波动率（示例因子，非策略）"
    category = "volatility"
    window_size = 21

    def compute(self, ctx: FactorContext) -> FactorResult:
        kl = ctx.klines or []
        base = dict(code=ctx.code, date=ctx.date, factor_name=self.name)
        if len(kl) < self.window_size:
            return FactorResult(**base, score=None, details={"skip": "K线不足"})
        rets = []
        for i in range(len(kl) - 20, len(kl)):
            p, c = _close(kl[i - 1]), _close(kl[i])
            if p <= 0 or c <= 0:
                return FactorResult(**base, score=None, details={"skip": "价格无效"})
            rets.append((c - p) / p)
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        v = math.sqrt(var)
        return FactorResult(**base, score=round(v, 6), raw_value=round(v, 6))

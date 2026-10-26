# -*- coding: utf-8 -*-
"""因子注册表。

**开源骨架说明**：本仓库只提供因子框架，**不含任何具体策略因子实现**。
下方只注册两个示例因子，用于跑通管线与演示接口。

接入自己的因子：
  1. 在 factors/ 下新建 your_factor.py，继承 BaseFactor，实现 compute()
  2. 在本文件 import 并加入 ALL_FACTORS
  3. 在 config 的 factor_weights 里分配权重

⚠️ 本仓库不含任何投资建议；示例因子不构成策略。
"""
from __future__ import annotations

from strategy_layer.factors.examples import (
    MomentumFactor20,
    VolatilityFactor20,
)

ALL_FACTORS = [
    MomentumFactor20,
    VolatilityFactor20,
]

# 示例等权；实际权重由使用者按自己的策略设定
DEFAULT_WEIGHTS: dict[str, float] = {
    "mom20": 0.5,
    "vol20": 0.5,
}

ALPHA_FACTORS: list = []  # 骨架不含 alpha 因子集


def get_factor_names() -> list[str]:
    return [f.name for f in ALL_FACTORS]


def get_factor_class(name: str):
    for f in ALL_FACTORS:
        if f.name == name:
            return f
    return None


def is_compute_only(factor_name: str) -> bool:
    """该因子是否只计算、不参与聚合。骨架默认 False。"""
    return factor_name in compute_only_factor_names()


def compute_only_factor_names() -> set[str]:
    """只在矩阵中计算、不参与 composite_score 聚合的因子名。

    骨架默认空集；使用者可按需返回。
    """
    return set()


__all__ = [
    "ALL_FACTORS", "DEFAULT_WEIGHTS", "ALPHA_FACTORS",
    "get_factor_names", "get_factor_class", "compute_only_factor_names",
]

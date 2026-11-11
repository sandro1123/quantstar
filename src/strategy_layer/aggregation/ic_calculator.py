"""IC Calculator — 因子IC计算

## Issue #6 口径定谳 (2026-10-30)

原实现两个真缺陷:

### A. `compute_ic` 手工算 Spearman, 并列值处理错
```python
rx = {v: i for i, v in enumerate(sorted(f_scores))}   # 值→名次
```
问题: **并列值不是平均秩** — scipy 用 midrank (并列第 3/4 名各记 3.5),
旧实现让后写者覆盖, 并列值被赋予不同名次 → 伪排序, 与 scipy 对拍不上。
修法: 直接调 `scipy.stats.spearmanr`, 全仓一个真源。

### B. `icir` 算的不是 ICIR
```python
icir = corr / np.std(forward_returns)   # 单期IC / 收益标准差
```
ICIR 的定义是**跨期**: `mean(IC_series) / std(IC_series)`。
拿单期 IC 除以收益标准差, 量纲与含义都不是 ICIR。
修法: 单期数据里 ICIR 不可算 → 置 None (宁缺毋滥); 真正的 ICIR 由
`compute_icir(ic_series)` 用跨期序列计算。旧指标保留但正名
`ic_over_return_std`, 防止下游继续当 ICIR 用。

### 缺失值策略 (issue 要求「写入文档并有测试」)
**成对删除 (pairwise deletion), 不填充。** 理由: 填充单边缺失会制造
虚假截面 (如用 0 或均值补收益), 让 IC 看起来更稳定。NaN 与无法转 float
的值一律剔除; 剔除后样本 < 10 则返回 0 / 不显著。
"""
from dataclasses import dataclass

import numpy as np
from scipy.stats import spearmanr


@dataclass
class ICResult:
    mean_ic: float
    std_ic: float
    icir: float | None          # 跨期 ICIR; 单期不可算 → None
    t_stat: float
    p_value: float
    is_significant: bool
    sample_size: int
    detail: dict | None = None
    # 诚实面: 旧实现叫 icir 的那个量, 正名为它本来的意思
    ic_over_return_std: float | None = None


class ICCalculator:
    def compute_ic(self, factor_scores: dict, forward_returns: dict) -> float:
        """Spearman rank IC — 与 scipy 逐位一致 (midrank, 并列值取平均秩)。

        Issue #6 验收: 「与 scipy.stats.spearmanr 对拍一致」。
        """
        codes = [c for c in factor_scores if c in forward_returns]
        if len(codes) < 10:
            return 0.0
        xs, ys = [], []
        for c in codes:
            f, r = factor_scores[c], forward_returns[c]
            if f is None or r is None:
                continue
            try:
                fx, ry = float(f), float(r)
            except (TypeError, ValueError):
                continue
            if np.isnan(fx) or np.isnan(ry):
                continue
            xs.append(fx)
            ys.append(ry)
        if len(xs) < 10:
            return 0.0
        corr, _ = spearmanr(xs, ys)
        return 0.0 if np.isnan(corr) else float(corr)

    def compute_ic_with_significance(
        self, factor_values: list[float], forward_returns: list[float]
    ) -> ICResult:
        """单截面 IC + t 检验。**ICIR 在单期不可算**, 故置 None。

        缺失值: NaN 成对剔除 (不填充) — 见模块 docstring。
        """
        x = np.array(factor_values, dtype=float)
        y = np.array(forward_returns, dtype=float)

        mask = ~(np.isnan(x) | np.isnan(y))
        x, y = x[mask], y[mask]
        n = len(x)

        if n < 10:
            return ICResult(
                mean_ic=0.0, std_ic=0.0, icir=None, t_stat=0.0,
                p_value=1.0, is_significant=False, sample_size=n,
                ic_over_return_std=None,
            )

        corr, p_value = spearmanr(x, y)
        corr = 0.0 if np.isnan(corr) else float(corr)
        p_value = 1.0 if np.isnan(p_value) else float(p_value)
        t_stat = corr * np.sqrt((n - 2) / (1 - corr**2)) if abs(corr) < 1 else 0.0
        std_y = float(np.std(y))
        ratio = float(corr / std_y) if std_y > 0 else None

        return ICResult(
            mean_ic=corr,
            std_ic=std_y,
            icir=None,          # 单期不可算 — 见 compute_icir
            t_stat=float(t_stat),
            p_value=p_value,
            is_significant=p_value < 0.05,
            sample_size=n,
            ic_over_return_std=ratio,
        )

    @staticmethod
    def compute_icir(ic_series: list[float]) -> float | None:
        """**真正的 ICIR** = mean(IC 序列) / std(IC 序列)。

        Issue #6: 「ICIR 分母与窗口」定义如下:
          - 输入 = 按时间排序的逐期 IC (每期一个截面 IC), 至少 10 期;
          - 分母用样本标准差 (ddof=1), 与业界惯例一致;
          - 期数不足 / 标准差为 0 / 全 NaN → None (宁缺毋滥, 不造 0)。
        """
        vals = []
        for v in (ic_series or []):
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if not np.isnan(fv):
                vals.append(fv)
        if len(vals) < 10:
            return None
        arr = np.array(vals, dtype=float)
        sd = float(np.std(arr, ddof=1))
        # 浮点容差: 全同序列的 np.std 可能返回 ~1e-18 而非精确 0,
        # 直接 ==0 会漏, 除出来是 6.9e15 这种天文数字 (实测踩过)。
        if sd <= 1e-12:
            return None
        return round(float(np.mean(arr) / sd), 4)

    def factor_rank_autocorrelation(
        self, factor_values_by_date: dict[str, list[float]]
    ) -> dict:
        """Compute factor rank autocorrelation (turnover/stability)"""
        dates = sorted(factor_values_by_date.keys())
        corrs = []
        for i in range(len(dates) - 1):
            today = np.array(factor_values_by_date[dates[i]], dtype=float)
            tomorrow = np.array(factor_values_by_date[dates[i + 1]], dtype=float)
            mask = ~(np.isnan(today) | np.isnan(tomorrow))
            if mask.sum() > 10:
                corr, _ = spearmanr(today[mask], tomorrow[mask])
                corrs.append(corr)

        return {
            "mean_autocorrelation": float(np.mean(corrs)) if corrs else 0.0,
            "std_autocorrelation": float(np.std(corrs)) if corrs else 0.0,
            "n_periods": len(corrs),
        }

    def compute_strategy_ic(
        self,
        composite_scores: list[float],
        forward_returns: list[float],
        factor_ics: list[dict],
    ) -> dict:
        """Compute strategy-level IC from composite scores"""
        x = np.array(composite_scores)
        y = np.array(forward_returns)
        mask = ~(np.isnan(x) | np.isnan(y))
        x, y = x[mask], y[mask]

        if len(x) < 10:
            return {"strategy_ic": 0.0, "sample_size": len(x), "is_significant": False}

        corr, p_value = spearmanr(x, y)
        n = len(x)
        t_stat = corr * np.sqrt((n - 2) / (1 - corr**2)) if abs(corr) < 1 else 0

        contributions = {}
        for fic in factor_ics:
            fname = fic.get("factor_name", "unknown")
            fic_val = fic.get("mean_ic", 0)
            contributions[fname] = fic_val

        sorted_factors = sorted(contributions.items(), key=lambda x: abs(x[1]), reverse=True)
        top_factors = [{"factor": f, "ic": round(v, 4)} for f, v in sorted_factors[:5]]

        return {
            "strategy_ic": round(float(corr), 4),
            "t_stat": round(float(t_stat), 4),
            "p_value": round(float(p_value), 4),
            "is_significant": p_value < 0.05,
            "sample_size": int(n),
            "factor_count": len(contributions),
            "top_factors": top_factors,
        }

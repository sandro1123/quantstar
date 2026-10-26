"""backtest_layer/evaluation/factor_drift.py — 因子分布漂移哨兵 (#57, b路线)

零新增依赖 (统筹 sm-832ad940cd864f31 裁 b): sklearn 原生四件
(PSI/KS/卡方/Wasserstein) 覆盖哨兵域 "因子分布漂移检测+告警"。
与卡I factor_decay 同域互补: decay=时滞衰减, drift=分布漂移。

聚合语义 (统筹授权裁量, sm-6fbbdccf2d584c70 采纳): triggered =
PSI OR KS OR CHI2 OR WASSERSTEIN 任一触发 — 四件量纲不同 (PSI
无量纲/p 值是概率/Wasserstein 是分布距离), 加权须先归一化=
引入自由参数, 哨兵域不值当; 与卡I decay_alerts 单阈值触发同族。

触发阈值 (业界惯例, 与 reference std 归一化后可比):
  PSI        > 0.25  显著漂移 (10 等频分箱, 基于 reference 分位)
  KS p值     < 0.01  两分布不同 (样本量 ~5000 时 p 值本身敏感,
                     故取 0.01 而非 0.05 — 哨兵降误报)
  卡方 p值   < 0.01  同上 (10 桶分箱计数)
  Wasserstein/ref_std > 1.0  平移幅度超过一个参考标准差
"""
from dataclasses import dataclass

import numpy as np
from scipy import stats

# 触发阈值 (哨兵口径, 常量在此单点定义)
PSI_TRIGGER = 0.25
PVALUE_TRIGGER = 0.01
WASSERSTEIN_TRIGGER = 1.0
_N_BINS = 10
_MIN_SAMPLES = 30  # 全指标样本下限, 少于则诚实返回不可评估
# 分箱族(PSI/卡方)样本下限: 每桶期望观测 >=5 (卡方经验规则)。
# H0 实测: current=30 时 PSI 假阳 70.7% (空桶 eps 垫底项一票
# 否决), 卡方 1.3% (expected=3<5 p 值失真); KS/W 非分箱族
# 30 样本下假阳率即名义水平 (KS 1.0% / W 0%), 不受此限。
_BINNED_MIN_SAMPLES = 5 * _N_BINS


@dataclass
class FactorDriftResult:
    factor: str
    psi: float | None = None
    ks_pvalue: float | None = None
    chi2_pvalue: float | None = None
    wasserstein_norm: float | None = None
    # None = 样本不足不可评估 (诚实, 不冒充不漂移); True/False = 判定
    triggered: bool | None = None


def _psi(reference: np.ndarray, current: np.ndarray) -> float:
    """PSI (Population Stability Index): 10 等频分箱基于 reference
    分位; 空桶 (计数为0) 以 epsilon 垫底防 log 爆炸。"""
    eps = 1e-6
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, _N_BINS + 1)))
    if len(edges) < 3:
        # 退化分布 (几乎全同值): 分不出箱, PSI 按无漂移处理 —
        # 同值分布的"漂移"由 KS/Wasserstein 补位判。
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    ref_counts = np.histogram(reference, bins=edges)[0]
    cur_counts = np.histogram(current, bins=edges)[0]
    ref_p = (ref_counts + eps) / (ref_counts.sum() + eps)
    cur_p = (cur_counts + eps) / (cur_counts.sum() + eps)
    return float(np.sum((cur_p - ref_p) * np.log(cur_p / ref_p)))


def compute_drift(factor: str, reference, current) -> FactorDriftResult:
    """四件漂移指标 + triggered=OR 聚合。

    reference/current: 该因子的历史截面值序列 / 当日截面值序列。
    样本不足 (<30 双侧) → triggered=None (不可评估, 不造假)。
    """
    ref = np.asarray(reference, dtype=float)
    cur = np.asarray(current, dtype=float)
    ref = ref[np.isfinite(ref)]
    cur = cur[np.isfinite(cur)]
    if len(ref) < _MIN_SAMPLES or len(cur) < _MIN_SAMPLES:
        return FactorDriftResult(factor=factor, triggered=None)

    res = FactorDriftResult(factor=factor)

    # 1. PSI — 分布结构漂移 (分箱稳定性)。
    #    分箱族小样本守卫: 桶期望观测不足 (<5/桶) 时 psi=None
    #    诚实跳过 — 小样本空桶 eps 垫底项结构性假阳 (H0 70.7%
    #    @30 实测), 不冒充任何方向的判定。
    binned_ok = len(ref) >= _BINNED_MIN_SAMPLES and len(cur) >= _BINNED_MIN_SAMPLES
    res.psi = _psi(ref, cur) if binned_ok else None

    # 2. KS — 两样本分布同一性
    ks_stat, ks_p = stats.ks_2samp(ref, cur)
    res.ks_pvalue = float(ks_p)

    # 3. 卡方 — 分箱计数齐一性 (与 PSI 同分箱族, 视角互补:
    #    PSI 看幅度, 卡方看显著性; expected<5 时 p 值近似失真,
    #    与 PSI 同守卫同跳)
    if not binned_ok:
        res.chi2_pvalue = None
    else:
        edges = np.unique(np.quantile(ref, np.linspace(0, 1, _N_BINS + 1)))
        if len(edges) >= 3:
            edges[0], edges[-1] = -np.inf, np.inf
            ref_counts = np.histogram(ref, bins=edges)[0]
            cur_counts = np.histogram(cur, bins=edges)[0]
            # 期望=current 总数按 reference 比例分布; scipy 期望须>0
            expected = cur_counts.sum() * (ref_counts / max(ref_counts.sum(), 1))
            mask = expected > 0
            if mask.sum() >= 2:
                chi2_stat, chi2_p = stats.chisquare(cur_counts[mask], expected[mask])
                res.chi2_pvalue = float(chi2_p)
            else:
                res.chi2_pvalue = 1.0  # 分不出有效箱 = 无证据, 诚实不触发
        else:
            res.chi2_pvalue = 1.0

    # 4. Wasserstein (归一化到 reference std — 跨因子可比)
    ref_std = float(np.std(ref))
    if ref_std > 1e-12:
        res.wasserstein_norm = float(stats.wasserstein_distance(ref, cur)) / ref_std
    else:
        res.wasserstein_norm = 0.0 if np.std(cur) < 1e-12 else None

    # OR 聚合 (统筹裁): 任一触发即证据
    triggered = False
    if res.psi is not None and res.psi > PSI_TRIGGER:
        triggered = True
    if res.ks_pvalue is not None and res.ks_pvalue < PVALUE_TRIGGER:
        triggered = True
    if res.chi2_pvalue is not None and res.chi2_pvalue < PVALUE_TRIGGER:
        triggered = True
    if res.wasserstein_norm is not None and res.wasserstein_norm > WASSERSTEIN_TRIGGER:
        triggered = True
    res.triggered = triggered
    return res

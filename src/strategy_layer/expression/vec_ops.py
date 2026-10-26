"""滚动/前缀算子的 numpy 向量化实现 — 刀4案2第⑦项主刀。

evaluate_series 原为纯 Python 逐 bar 解释器: 滚动算子每 bar 重算
func(values[:i+1], window) (O(n×window) + 列表切片), alpha_split 实测
902ms/股 占 alpha 成本 100%, 是 compute_all 退化的真大头。

本模块每个函数与 operator_lib 标量实现逐位数值等价 (容差阈值见
docs/alpha-vec-tolerance.md), 边界语义全覆盖: 部分窗口 / 重复值 /
除零 / 空序列 / window>n / window=1。

契约:
- 输入 values 为 list[float] (parser _align 后长度 n), 输出 list[float] 长度 n;
- 返回 None = 向量化路径放弃处理, 调用方回退原逐 bar 标量循环 (零行为变更)。

数值方法选择: 统计类算子 (stddev/zscore/skew/kurt/corr/cov/ts_rank) 用
两遍法 + sliding_window_view, 逐位复刻标量算法的数值行为 — 不用 cumsum
单遍法, 因价格 ~1e3 量级下高阶矩的 catastrophic cancellation 会超容差
(前段 cumsum 单遍在常数序列上实测 8.4e-08 超差, 已改两遍法)。

退化方差守卫: 常数/近常数序列 (A股一字板) 的浮点舍入使 var 不为精确 0,
标量 `== 0` 守卫漏触发进入 0/0 未定义区输出垃圾值。本模块与 operator_lib
标量侧同用 _VAR_EPS=1e-12 守卫 (统筹裁定双侧修复), 见容差文档安全性论证。
"""
import numpy as np

# 退化方差守卫阈值, 与 operator_lib.ts_operators 同源 (单一事实源)。
# _VAR_EPS 用于 corr (标量守卫 var), _STD_EPS 用于 zscore/skew/kurt (标量守卫 std)。
from strategy_layer.operator_lib.ts_operators import _STD_EPS, _VAR_EPS


def _arr(values) -> np.ndarray:
    return np.asarray(values, dtype=np.float64)


def _sliding(v: np.ndarray, window: int) -> np.ndarray:
    return np.lib.stride_tricks.sliding_window_view(v, window)


def _seq_window_sum(v: np.ndarray, window: int) -> np.ndarray:
    """整窗求和, 与 Python sum(v[i-w+1:i+1]) 逐位 bit-identical。

    numpy `.sum(axis=1)` 用 pairwise 加法, 舍入顺序与 Python 从左到右的
    顺序累加不同 (~1e-16 级差异) — 足以翻转下游 rank 的浮点精度极限 tie
    (alpha_005 bar102 实证: ts_sum 扰动 1.3e-12 翻转 8.9e-16 的 tie)。
    改为跨 bar 逐列元素级累加: 每步是一次 numpy add (逐元素精确, 与标量
    同序), 结果 bit-identical 且仍保持向量化 (w 次向量加法, C 层执行)。"""
    m = len(v) - window + 1
    acc = v[:m].copy()
    for j in range(1, window):
        acc = acc + v[j:j + m]
    return acc


def _seq_rows_sum(a: np.ndarray) -> np.ndarray:
    """2D 沿 window 轴 (axis=1) 顺序求和, 逐行 bit-identical 于 Python sum(row)。
    每步是一次逐元素精确的 numpy add, 与标量从左到右累加同序同精度。"""
    acc = a[:, 0].copy()
    for j in range(1, a.shape[1]):
        acc = acc + a[:, j]
    return acc


# ── ROLLING1: 单序列滚动算子 ─────────────────────────────────────────────


def vec_ts_sum(values, window):
    """与标量 sum(values[-window:]) bit-identical (顺序累加, 见 _seq_window_sum)。
    前缀段 np.cumsum 即从左到右顺序累加, 与 Python sum(前缀) 同序同精度。"""
    n = len(values)
    if n == 0 or window <= 0:
        return [0.0] * n
    v = _arr(values)
    out = np.empty(n)
    pre = min(window - 1, n)
    if pre > 0:
        out[:pre] = np.cumsum(v)[:pre]  # 前缀段与标量 sum 同序累加
    if n >= window:
        out[window - 1:] = _seq_window_sum(v, window)
    return out.tolist()


def vec_ts_min(values, window):
    n = len(values)
    if n == 0 or window <= 0:
        return [0.0] * n
    v = _arr(values)
    out = np.empty(n)
    pre = min(window - 1, n)  # 部分窗口: values[0:i+1] 的前缀 min
    if pre > 0:
        out[:pre] = np.minimum.accumulate(v)[:pre]
    if n >= window:
        out[window - 1:] = _sliding(v, window).min(axis=1)
    return out.tolist()


def vec_ts_max(values, window):
    n = len(values)
    if n == 0 or window <= 0:
        return [0.0] * n
    v = _arr(values)
    out = np.empty(n)
    pre = min(window - 1, n)
    if pre > 0:
        out[:pre] = np.maximum.accumulate(v)[:pre]
    if n >= window:
        out[window - 1:] = _sliding(v, window).max(axis=1)
    return out.tolist()


def vec_ts_argmax(values, window):
    n = len(values)
    if n == 0 or window <= 0:
        return [-1.0] * n  # 标量 ts_argmax 空/无效窗返回 -1
    v = _arr(values)
    out = np.empty(n)
    pre = min(window - 1, n)
    for i in range(pre):  # 部分窗口最多 window-1 个 bar, 小循环可接受
        out[i] = float(v[: i + 1].argmax())  # numpy argmax 返回首个最大值
    if n >= window:
        out[window - 1:] = _sliding(v, window).argmax(axis=1)
    return out.tolist()


def vec_ts_argmin(values, window):
    n = len(values)
    if n == 0 or window <= 0:
        return [-1.0] * n
    v = _arr(values)
    out = np.empty(n)
    pre = min(window - 1, n)
    for i in range(pre):
        out[i] = float(v[: i + 1].argmin())
    if n >= window:
        out[window - 1:] = _sliding(v, window).argmin(axis=1)
    return out.tolist()


def _prefix_two_pass(v: np.ndarray, pre: int):
    """部分窗口 (长度 1..pre) 的两遍法 mean/std/m3/m4, 逐位复刻标量算法:
    mean = sum(seg)/len (顺序累加), 矩 = sum((x-mean)**k)/len。
    求和用 cumsum (严格从左到右扫描, 与 Python sum 逐步同舍入, bit-identical;
    不用 numpy 1D .sum() — 元素较多时走 pairwise 加法, 舍入顺序不同)。
    不用 cumsum 单遍法算矩: 二阶及以上矩在常数序列上 catastrophic
    cancellation (实测 8.4e-08 超差)。pre<=window-1, 小循环。"""
    means = np.empty(pre)
    stds = np.empty(pre)
    m3s = np.empty(pre)
    m4s = np.empty(pre)
    c = np.cumsum(v)  # c[i] = Python sum(v[:i+1]) bit-identical
    for i in range(pre):
        m = i + 1
        mu = float(c[i]) / m
        d = v[:m] - mu
        m2 = float(np.cumsum(d * d)[-1]) / m
        means[i] = mu
        stds[i] = np.sqrt(m2)
        m3s[i] = float(np.cumsum(d ** 3)[-1]) / m
        m4s[i] = float(np.cumsum(d ** 4)[-1]) / m
    return means, stds, m3s, m4s


def vec_ts_stddev(values, window):
    n = len(values)
    if n == 0 or window <= 1:  # 标量: window<=1 → 0.0
        return [0.0] * n
    v = _arr(values)
    out = np.zeros(n)
    pre = min(window - 1, n)
    if pre > 0:
        _, stds, _, _ = _prefix_two_pass(v, pre)
        out[:pre] = stds
    if n >= window:
        wins = _sliding(v, window)
        mu = _seq_rows_sum(wins) / window  # 顺序累加 = 标量 sum(recent)/len
        d = wins - mu[:, None]
        var = _seq_rows_sum(d * d) / window
        out[window - 1:] = np.sqrt(var)  # 两遍法=标量同算法同序
    return out.tolist()


def vec_ts_zscore(values, window):
    n = len(values)
    if n == 0 or window <= 1:
        return [0.0] * n
    v = _arr(values)
    out = np.zeros(n)
    pre = min(window - 1, n)
    if pre > 0:
        means, stds, _, _ = _prefix_two_pass(v, pre)
        safe = np.where(stds < _STD_EPS, 1.0, stds)
        out[:pre] = np.where(stds < _STD_EPS, 0.0, (v[:pre] - means) / safe)
    if n >= window:
        wins = _sliding(v, window)
        mu = _seq_rows_sum(wins) / window  # 顺序累加 = 标量 sum(recent)/len
        d = wins - mu[:, None]
        m2 = _seq_rows_sum(d * d) / window
        std = np.sqrt(m2)
        last = wins[:, -1]
        safe = np.where(std < _STD_EPS, 1.0, std)
        out[window - 1:] = np.where(std < _STD_EPS, 0.0, (last - mu) / safe)
    return out.tolist()


def vec_ts_skew(values, window):
    n = len(values)
    if n == 0 or window < 3:  # 标量: window<3 → 0.0
        return [0.0] * n
    v = _arr(values)
    out = np.zeros(n)
    pre = min(window - 1, n)
    if pre > 0:
        # 前缀段按实际长度 m=i+1 计算 (标量按传入 window 判 <3, 此处 window>=3),
        # skew = mean((x-mu)^3)/std^3, 退化方差守卫 std<_STD_EPS → 0.0 (镜像标量)
        _, stds, m3s, _ = _prefix_two_pass(v, pre)
        safe = np.where(stds < _STD_EPS, 1.0, stds)
        out[:pre] = np.where(stds < _STD_EPS, 0.0, m3s / safe ** 3)
    if n >= window:
        wins = _sliding(v, window)
        mu = _seq_rows_sum(wins) / window  # 顺序累加 = 标量 sum(recent)/n
        d = wins - mu[:, None]
        m2 = _seq_rows_sum(d * d) / window
        m3 = _seq_rows_sum(d ** 3) / window
        std = np.sqrt(m2)
        safe = np.where(std < _STD_EPS, 1.0, std)
        out[window - 1:] = np.where(std < _STD_EPS, 0.0, m3 / safe ** 3)
    return out.tolist()


def vec_ts_kurt(values, window):
    n = len(values)
    if n == 0 or window < 4:  # 标量: window<4 → 0.0
        return [0.0] * n
    v = _arr(values)
    out = np.zeros(n)
    pre = min(window - 1, n)
    if pre > 0:
        _, stds, _, m4s = _prefix_two_pass(v, pre)
        # 标量: sum((x-mean)**4)/(n*std**4) - 3 → 用 std**4 逐位复刻
        safe = np.where(stds < _STD_EPS, 1.0, stds)
        out[:pre] = np.where(stds < _STD_EPS, 0.0, m4s / safe ** 4 - 3.0)
    if n >= window:
        wins = _sliding(v, window)
        mu = _seq_rows_sum(wins) / window  # 顺序累加 = 标量 sum(recent)/n
        d = wins - mu[:, None]
        m2 = _seq_rows_sum(d * d) / window
        m4 = _seq_rows_sum(d ** 4) / window
        std = np.sqrt(m2)
        # 标量: sum((x-mean)**4)/(n*std**4) - 3 → 用 std**4 (非 var**2) 逐位复刻
        safe = np.where(std < _STD_EPS, 1.0, std)
        out[window - 1:] = np.where(std < _STD_EPS, 0.0, m4 / safe ** 4 - 3.0)
    return out.tolist()


def vec_ts_rank(values, window):
    """窗内末位排位。标量语义: sorted 后 dict 覆盖 = count(<=v)-1 位,
    值 = 位 / max(m-1, 1), m=窗内实际长度。"""
    n = len(values)
    if n == 0 or window <= 0:
        return [0.0] * n
    v = _arr(values)
    out = np.zeros(n)
    pre = min(window - 1, n)
    if pre > 0:
        pv = v[:pre]
        ge = pv[:, None] >= pv[None, :]  # ge[i,j] = v[i]>=v[j]
        counts = np.tril(ge).sum(axis=1)  # 前缀内 count(v[j]<=v[i])
        denom = np.maximum(np.arange(1, pre + 1) - 1, 1)
        out[:pre] = (counts - 1) / denom
    if n >= window:
        wins = _sliding(v, window)
        last = wins[:, -1:]
        counts = (wins <= last).sum(axis=1)
        out[window - 1:] = (counts - 1) / max(window - 1, 1)
    return out.tolist()


def vec_ts_decaylinear(values, window):
    """权重 1..m (m=窗内实际长度) 的加权平均。ts_wma 同义。"""
    n = len(values)
    if n == 0 or window <= 0:
        return [0.0] * n
    v = _arr(values)
    out = np.zeros(n)
    pre = min(window - 1, n)
    if pre > 0:
        w = np.arange(1, pre + 1, dtype=np.float64)
        cw = np.cumsum(w * v[:pre])
        out[:pre] = cw / (w * (w + 1) / 2)
    if n >= window:
        wins = _sliding(v, window)
        wts = np.arange(1, window + 1, dtype=np.float64)
        # 标量: sum(w*v for w,v in zip(weights, recent)) 顺序累加 → 逐位复刻
        out[window - 1:] = _seq_rows_sum(wins * wts) / (window * (window + 1) / 2)
    return out.tolist()


# ── ROLLING2: 双序列滚动算子 ─────────────────────────────────────────────


def vec_ts_corr(a, b, window):
    """滚动相关。标量要求整窗 (len<window → 0.0), 退化方差 → 0.0。
    window<=0 时标量有 values[-0:]=全前缀 的病态语义 → 返回 None 回退。"""
    n = len(a)
    if window <= 0:
        return None
    if n == 0:
        return []
    out = np.zeros(n)
    if n < window:
        return out.tolist()
    x = _arr(a)
    y = _arr(b)
    wx = _sliding(x, window)
    wy = _sliding(y, window)
    mx = _seq_rows_sum(wx) / window  # 顺序累加 = 标量 sum(a)/n
    my = _seq_rows_sum(wy) / window
    dx = wx - mx[:, None]
    dy = wy - my[:, None]
    var_a = _seq_rows_sum(dx * dx)  # 两遍法平方和 (非负), 与标量同序
    var_b = _seq_rows_sum(dy * dy)
    cov = _seq_rows_sum(dx * dy)
    denom = np.sqrt(var_a * var_b)  # 标量: math.sqrt(var_a * var_b)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where((var_a < _VAR_EPS) | (var_b < _VAR_EPS), 0.0,
                     cov / np.where(denom > 0, denom, 1.0))
    out[window - 1:] = r
    return out.tolist()


def vec_ts_covariance(a, b, window):
    """滚动协方差 = cov/window。标量要求整窗, 无零方差守卫 (cov 自然为 0)。"""
    n = len(a)
    if window <= 0:
        return None
    if n == 0:
        return []
    out = np.zeros(n)
    if n < window:
        return out.tolist()
    x = _arr(a)
    y = _arr(b)
    wx = _sliding(x, window)
    wy = _sliding(y, window)
    mx = _seq_rows_sum(wx) / window  # 顺序累加 = 标量 sum(a)/n
    my = _seq_rows_sum(wy) / window
    dx = wx - mx[:, None]
    dy = wy - my[:, None]
    out[window - 1:] = _seq_rows_sum(dx * dy) / window
    return out.tolist()


# ── SHIFT: 位移/差分算子 ─────────────────────────────────────────────────


def vec_ts_delay(values, d):
    """标量语义: d<=0 → 当前值; d>=len(前缀) → 0.0; 否则 values[-(d+1)]。"""
    n = len(values)
    if n == 0:
        return []
    v = _arr(values)
    if d <= 0:
        return v.tolist()
    out = np.zeros(n)
    if n > d:
        out[d:] = v[: n - d]
    return out.tolist()


def vec_ts_delta(values, d):
    """标量语义: d<=0 → 0.0; d>=len(前缀) → 0.0; 否则 v[-1]-v[-(d+1)]。"""
    n = len(values)
    if n == 0 or d <= 0:
        return [0.0] * n
    v = _arr(values)
    out = np.zeros(n)
    if n > d:
        out[d:] = v[d:] - v[: n - d]
    return out.tolist()


# ── PREFIX: 前缀末位算子 ─────────────────────────────────────────────────


def vec_rank_prefix(values):
    """前缀末位排位。标量 cs_operators.rank: sorted 前缀后 dict 覆盖,
    值 = (count(<=v)-1) / max(len-1, 1)。比较矩阵法 (n<=250, 62.5KB)。"""
    n = len(values)
    if n == 0:
        return []
    v = _arr(values)
    ge = v[:, None] >= v[None, :]  # ge[i,j] = v[i]>=v[j] 即 v[j]<=v[i]
    counts = np.tril(ge).sum(axis=1)  # 前缀内 j<=i 的 count
    denom = np.maximum(np.arange(n), 1)
    return ((counts - 1) / denom).tolist()


def vec_scale(values, target=1.0):
    """标量: last / sum(abs(前缀)) * target; 分母为 0 → 0.0。"""
    n = len(values)
    if n == 0:
        return []
    v = _arr(values)
    cumabs = np.cumsum(np.abs(v))
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(cumabs == 0, 0.0, v / np.where(cumabs == 0, 1.0, cumabs) * target)
    return out.tolist()


def vec_demean(values):
    """标量: last - mean(前缀)。"""
    n = len(values)
    if n == 0:
        return []
    v = _arr(values)
    c = np.cumsum(v)
    m = np.arange(1, n + 1)
    return (v - c / m).tolist()


def vec_adv20(values):
    """标量: sum(前缀末 20 个)/len。前缀段 (i<20) 用 cumsum (顺序累加,
    与 Python sum 同序); 整窗段用 _seq_window_sum — 不用 cumsum 差分,
    全程前缀累加再相减有 cancellation, 与标量 20 元素顺序求和不同位。"""
    n = len(values)
    if n == 0:
        return []
    v = _arr(values)
    out = np.empty(n)
    pre = min(20, n)
    if pre > 0:
        out[:pre] = np.cumsum(v)[:pre] / np.arange(1, pre + 1)
    if n >= 20:
        out[19:] = _seq_window_sum(v, 20) / 20
    return out.tolist()


# ── SCALAR: 逐 bar 标量算子 (parser 兜底分支的向量化) ────────────────────


def _vec_sign(x):
    x = _arr(x)
    # 标量 helper_ops.sign: nan>0/nan<0 均 False → 0.0; np.sign(nan)=nan 需守卫
    return np.where(np.isnan(x), 0.0, np.sign(x))


def _vec_log(x):
    x = _arr(x)
    # 标量 helper_ops.log: x<=0 → 0.0 (nan<=0 为 False → math.log(nan)=nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(x <= 0, 0.0, np.log(np.where(x > 0, x, 1.0)))


def _vec_abs(x):
    return np.abs(_arr(x))


def _vec_signedpower(x, power):
    # 与 helper_ops.signedpower 同一 numpy 算式 (标量内部本就走 numpy)
    x = _arr(x)
    with np.errstate(all="ignore"):
        return np.sign(x) * np.abs(x) ** power


# ── 注册表 (parser.evaluate_series 按算子名查表) ─────────────────────────

ROLLING1 = {
    "ts_sum": vec_ts_sum,
    "ts_rank": vec_ts_rank,
    "ts_stddev": vec_ts_stddev,
    "stddev": vec_ts_stddev,
    "ts_zscore": vec_ts_zscore,
    "ts_min": vec_ts_min,
    "ts_max": vec_ts_max,
    "ts_argmax": vec_ts_argmax,
    "ts_argmin": vec_ts_argmin,
    "ts_skew": vec_ts_skew,
    "ts_kurt": vec_ts_kurt,
    "ts_decaylinear": vec_ts_decaylinear,
    "ts_wma": vec_ts_decaylinear,
}

ROLLING2 = {
    "ts_corr": vec_ts_corr,
    "correlation": vec_ts_corr,
    "ts_covariance": vec_ts_covariance,
    "covariance": vec_ts_covariance,
}

SHIFT = {
    "ts_delay": vec_ts_delay,
    "delay": vec_ts_delay,
    "ts_delta": vec_ts_delta,
    "delta": vec_ts_delta,
}

PREFIX = {
    "rank": vec_rank_prefix,
    "scale": vec_scale,
    "demean": vec_demean,
    # adv20 无 window 参数、前缀末位语义同族, 挂此表供 parser adv20 分支查表
    "adv20": vec_adv20,
}

SCALAR = {
    "sign": _vec_sign,
    "log": _vec_log,
    "abs": _vec_abs,
    "signedpower": _vec_signedpower,
}

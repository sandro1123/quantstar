"""Alpha48 factor: 48 个量价因子集成 (WorldQuant-style)"""
import math

from strategy_layer.factors.base import BaseFactor, FactorContext, FactorResult


def _extract(klines, field):
    return [k[field] if isinstance(k, dict) else getattr(k, field) for k in klines]


def _rolling_mean(values, w):
    n = len(values)
    res = [0.0] * n
    cum = 0.0
    for i in range(n):
        cum += values[i]
        if i >= w:
            cum -= values[i - w]
        if i >= w - 1:
            res[i] = cum / w
    return res


def _rolling_std(values, w, means=None):
    n = len(values)
    if means is None:
        means = _rolling_mean(values, w)
    res = [0.0] * n
    for i in range(w - 1, n):
        s = sum((values[j] - means[i]) ** 2 for j in range(i - w + 1, i + 1))
        res[i] = math.sqrt(s / w)
    return res


def _rolling_sum(values, w):
    n = len(values)
    res = [0.0] * n
    cum = 0.0
    for i in range(n):
        cum += values[i]
        if i >= w:
            cum -= values[i - w]
        if i >= w - 1:
            res[i] = cum
    return res


def _rolling_max(values, w):
    n = len(values)
    res = [0.0] * n
    for i in range(w - 1, n):
        res[i] = max(values[i - w + 1:i + 1])
    return res


def _rolling_min(values, w):
    n = len(values)
    res = [0.0] * n
    for i in range(w - 1, n):
        res[i] = min(values[i - w + 1:i + 1])
    return res


def _ema(values, span):
    n = len(values)
    res = [0.0] * n
    alpha = 2.0 / (span + 1)
    res[0] = values[0]
    for i in range(1, n):
        res[i] = alpha * values[i] + (1 - alpha) * res[i - 1]
    return res


ALL_48_FACTOR_NAMES = [
    "kmid", "klen", "kmid2", "kup", "kup2", "klow", "klow2", "ksft", "ksft2",
    "roc5", "roc10", "roc20", "roc60",
    "ma5_dev", "ma10_dev",
    "std5", "std10", "std20",
    "max5", "min5", "rank5", "rank10",
    "corr5", "corr10", "cord5", "cord20",
    "cntp5", "cntp10", "cntp20", "cntd5", "cntd10", "cntd20",
    "sump5", "sump10", "sump20", "sumd5", "sumd10", "sumd20",
    "vma5", "vma10", "vma20", "vstd5", "vstd10",
    "rsi14", "macd_hist", "bb_width",
    "alpha010", "alpha101",
]


class Alpha48Factor(BaseFactor):
    name = "alpha_48"
    description = "48 个量价因子简单集成"
    category = "technical"
    window_size = 65

    def compute(self, ctx: FactorContext) -> FactorResult:
        closes = _extract(ctx.klines, "close")
        highs = _extract(ctx.klines, "high")
        lows = _extract(ctx.klines, "low")
        opens_ = _extract(ctx.klines, "open")
        volumes = _extract(ctx.klines, "volume")

        n = len(closes)
        eps = 1e-8
        if n < 10:
            return FactorResult(code=ctx.code, date=ctx.date, factor_name=self.name)

        returns = [0.0] * n
        vol_returns = [0.0] * n
        for i in range(1, n):
            denom = max(eps, abs(closes[i - 1]))
            returns[i] = (closes[i] - closes[i - 1]) / denom
            denom_v = max(eps, volumes[i - 1])
            vol_returns[i] = (volumes[i] - volumes[i - 1]) / denom_v

        abs_ret = [abs(r) for r in returns]
        abs_vr = [abs(vr) for vr in vol_returns]

        factors = {}

        c, h, lo, o = closes[-1], highs[-1], lows[-1], opens_[-1]
        denom_o = max(eps, abs(o))
        denom_hl = max(eps, h - lo)
        factors["kmid"] = (c - o) / denom_o
        factors["klen"] = (h - lo) / denom_o
        factors["kmid2"] = (c - o) / denom_hl
        factors["kup"] = (h - max(o, c)) / denom_o
        factors["kup2"] = (h - max(o, c)) / denom_hl
        factors["klow"] = (min(o, c) - lo) / denom_o
        factors["klow2"] = (min(o, c) - lo) / denom_hl
        factors["ksft"] = (2 * c - h - lo) / denom_o
        factors["ksft2"] = (2 * c - h - lo) / denom_hl

        def v(arr):
            return float(arr[-1]) if arr and len(arr) > 0 else 0.0

        for w, name_w in [(5, 5), (10, 10), (20, 20), (60, 60)]:
            if n >= w:
                factors[f"roc{name_w}"] = v(_rolling_sum(returns, w))
                factors[f"std{name_w}"] = v(_rolling_std(returns, w))
                rmax = _rolling_max(closes, w)
                rmin = _rolling_min(closes, w)
                factors[f"max{name_w}"] = v(rmax)
                factors[f"min{name_w}"] = v(rmin)
                rank = (closes[-1] - rmin[-1]) / (rmax[-1] - rmin[-1]) if (rmax[-1] - rmin[-1]) > eps else 0.0
                factors[f"rank{name_w}"] = rank

        ma5 = _rolling_mean(closes, 5)
        ma10 = _rolling_mean(closes, 10)
        factors["ma5_dev"] = (closes[-1] - ma5[-1]) / (max(eps, abs(ma5[-1])))
        factors["ma10_dev"] = (closes[-1] - ma10[-1]) / (max(eps, abs(ma10[-1])))

        for w, name_w in [(5, 5), (10, 10)]:
            if n >= w:
                r_slice = returns[n-w:n]
                vr_slice = vol_returns[n-w:n]
                r_mean = sum(r_slice) / w
                vr_mean = sum(vr_slice) / w
                corr_num = sum((r_slice[i] - r_mean) * (vr_slice[i] - vr_mean) for i in range(w))
                ar_slice = abs_ret[n-w:n]
                av_slice = abs_vr[n-w:n]
                ar_mean = sum(ar_slice) / w
                av_mean = sum(av_slice) / w
                cord_num = sum((ar_slice[i] - ar_mean) * (av_slice[i] - av_mean) for i in range(w))
                std_r = _rolling_std(returns, w)
                std_vr = _rolling_std(vol_returns, w)
                denom_corr = w * std_r[-1] * std_vr[-1]
                factors[f"corr{name_w}"] = corr_num / denom_corr if denom_corr > eps else 0.0

        for w, name_w in [(5, 5), (20, 20)]:
            if n >= w:
                ar_slice = abs_ret[n-w:n]
                av_slice = abs_vr[n-w:n]
                ar_mean = sum(ar_slice) / w
                av_mean = sum(av_slice) / w
                cord_num = sum((ar_slice[i] - ar_mean) * (av_slice[i] - av_mean) for i in range(w))
                std_r = _rolling_std(returns, w)
                std_vr = _rolling_std(vol_returns, w)
                denom_cord = w * std_r[-1] * std_vr[-1]
                factors[f"cord{name_w}"] = cord_num / denom_cord if denom_cord > eps else 0.0

        for w, name_w in [(5, 5), (10, 10), (20, 20)]:
            if n >= w:
                window_ret = returns[n - w + 1:n + 1]
                up_cnt = sum(1 for r in window_ret if r > 0)
                dn_cnt = sum(1 for r in window_ret if r < 0)
                total = up_cnt + dn_cnt
                factors[f"cntp{name_w}"] = up_cnt / total if total > 0 else 0.0
                factors[f"cntd{name_w}"] = (up_cnt - dn_cnt) / total if total > 0 else 0.0
                sum_up = sum(r for r in window_ret if r > 0)
                sum_dn = sum(abs(r) for r in window_ret if r < 0)
                total_sum = sum_up + sum_dn
                factors[f"sump{name_w}"] = sum_up / total_sum if total_sum > eps else 0.0
                factors[f"sumd{name_w}"] = (sum_up - sum_dn) / total_sum if total_sum > eps else 0.0

        for w, name_w in [(5, 5), (10, 10), (20, 20)]:
            if n >= w:
                factors[f"vma{name_w}"] = v(_rolling_mean(volumes, w))
                factors[f"vstd{name_w}"] = v(_rolling_std(volumes, w))

        if n >= 14:
            gain = [max(0, r) for r in returns]
            loss = [-r if r < 0 else 0 for r in returns]
            ag = _rolling_mean(gain, 14)
            al = _rolling_mean(loss, 14)
            factors["rsi14"] = 100 - 100 / (1 + ag[-1] / al[-1]) if al[-1] > eps else 100.0

        if n >= 26:
            e12 = _ema(closes, 12)
            e26 = _ema(closes, 26)
            macd_line = [e12[i] - e26[i] for i in range(n)]
            signal = _ema(macd_line, 9)
            factors["macd_hist"] = v([macd_line[i] - signal[i] for i in range(n)])

        if n >= 20:
            bbm = _rolling_mean(closes, 20)
            bbs = _rolling_std(closes, 20)
            denom_bb = max(eps, abs(bbm[-1]))
            factors["bb_width"] = ((bbm[-1] + 2 * bbs[-1]) - (bbm[-1] - 2 * bbs[-1])) / denom_bb

        factors["alpha010"] = (closes[-1] - closes[-2]) * (volumes[-1] - volumes[-2]) if n >= 2 else 0.0
        factors["alpha101"] = (closes[-1] - opens_[-1]) / (highs[-1] - lows[-1]) if (highs[-1] - lows[-1]) > 0.001 else 0.0

        scores = [v for v in factors.values() if not (math.isnan(v) or math.isinf(v))]
        avg = sum(scores) / len(scores) if scores else 0.0

        return FactorResult(
            code=ctx.code, date=ctx.date, factor_name=self.name,
            score=avg, raw_value=avg,
            details=factors,
        )
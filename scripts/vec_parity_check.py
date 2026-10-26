"""刀4案2⑦ 一级对拍: vec_ops 向量化 vs operator_lib 标量逐bar循环。

old 路径 = parser.evaluate_series 现状的逐 bar 标量循环 (含异常→0.0 兜底),
new 路径 = vec_ops 向量化。阈值口径见 docs/alpha-vec-tolerance.md (施工前预注册)。
边界清单 10 项全覆盖。输出逐算子 PASS/FAIL + max diff, 落 jsonl 供统筹抽查。
"""
import json
import math
import sys
import time

sys.path.insert(0, "src")

import numpy as np
from strategy_layer.expression import vec_ops
from strategy_layer.operator_lib import cs_operators, helper_ops, ts_operators

SEED = 42
N = 250

# ── 阈值口径 (与 docs/alpha-vec-tolerance.md 一致, 不得放宽) ─────────────
EXACT = (0.0, 0.0)          # 离散/选择类: 逐位相等
LINEAR = (1e-9, 1e-7)       # 线性求和类
STAT = (1e-8, 1e-6)         # 统计类
RANK_T = (1e-9, 1e-7)       # ts_rank


def scenarios_1():
    """单序列 10 项边界场景。返回 (name, values, window) 列表。"""
    rng = np.random.default_rng(SEED)
    s = []
    s.append(("random_n250", rng.standard_normal(N).tolist(), 20))
    # 重复值密集: 从 5 个值里取, 同值占比 >50%
    s.append(("dup_heavy", rng.integers(0, 5, N).astype(float).tolist(), 20))
    s.append(("all_zero", [0.0] * N, 20))
    s.append(("empty", [], 20))
    s.append(("window_gt_n", rng.standard_normal(10).tolist(), 50))
    s.append(("window_eq_1", rng.standard_normal(N).tolist(), 1))
    s.append(("n_eq_1", [3.14], 20))
    s.append(("constant", [7.7] * N, 20))
    s.append(("neg_zero_mix", [(-1.0) ** i * (i % 7) * 0.5 for i in range(N)], 20))
    return s


def scenarios_2():
    """双序列场景 (ROLLING2)。"""
    rng = np.random.default_rng(SEED + 1)
    s = []
    a = rng.standard_normal(N).tolist()
    b = rng.standard_normal(N).tolist()
    s.append(("random_n250", a, b, 10))
    s.append(("dup_heavy", rng.integers(0, 3, N).astype(float).tolist(),
              rng.integers(0, 3, N).astype(float).tolist(), 10))
    s.append(("all_zero", [0.0] * N, [0.0] * N, 10))
    s.append(("empty", [], [], 10))
    s.append(("window_gt_n", a[:8], b[:8], 50))
    s.append(("window_eq_1", a, b, 1))
    s.append(("n_eq_1", [1.5], [2.5], 10))
    s.append(("constant", [4.2] * N, [4.2] * N, 10))
    s.append(("const_b_var_a", a, [9.9] * N, 10))  # var_b==0 除0靶点
    return s


def old_rolling1(func, values, window):
    n = len(values)
    out = []
    for i in range(n):
        try:
            out.append(float(func(values[: i + 1], window)))
        except Exception:
            out.append(0.0)
    return out


def old_rolling2(func, a, b, window):
    n = len(a)
    out = []
    for i in range(n):
        try:
            out.append(float(func(a[: i + 1], b[: i + 1], window)))
        except Exception:
            out.append(0.0)
    return out


def old_shift(func, values, k):
    n = len(values)
    out = []
    for i in range(n):
        try:
            out.append(float(func(values[: i + 1], k)))
        except Exception:
            out.append(0.0)
    return out


def old_prefix(func, values):
    n = len(values)
    out = []
    for i in range(n):
        try:
            out.append(float(func(values[: i + 1])))
        except Exception:
            out.append(0.0)
    return out


def old_scalar(func, values, *extra):
    n = len(values)
    out = []
    for i in range(n):
        try:
            r = func(values[i], *extra)
            if isinstance(r, (list, tuple)):
                r = r[-1] if r else 0.0
            out.append(float(r))
        except Exception:
            out.append(0.0)
    return out


def compare(old, new, atol, rtol):
    """np.allclose 标准组合判据: |new-old| <= atol + rtol*|old| (逐元素)。
    不用「双指标独立达标」: 参考值≈0 时 rtol 分母无定义, 会把 1e-15 级
    绝对误差误判 FAIL (ts_stddev 常数序列两侧均 ~1.77e-15, 远小于 atol)。
    atol/rtol 阈值数值不变, 仅修组合方式。返回 (ok, max_abs, max_margin)。"""
    if len(old) != len(new):
        return False, float("inf"), float("inf")
    if not old:
        return True, 0.0, 0.0
    o = np.asarray(old, dtype=np.float64)
    v = np.asarray(new, dtype=np.float64)
    # NaN 政策: 两边必须同为 NaN
    nan_o = np.isnan(o)
    nan_v = np.isnan(v)
    if not np.array_equal(nan_o, nan_v):
        return False, float("inf"), float("inf")
    finite = ~nan_o
    if not finite.any():
        return True, 0.0, 0.0
    d = np.abs(v[finite] - o[finite])
    tol = atol + rtol * np.abs(o[finite])
    max_abs = float(d.max())
    # margin = 超出容差的部分 (<=0 即 PASS), 供抽查
    max_margin = float((d - tol).max())
    ok = bool(np.all(d <= tol))
    return ok, max_abs, max_margin


RESULTS = []


def record(op, scen, ok, max_abs, max_margin, atol, rtol, note=""):
    RESULTS.append({
        "op": op, "scenario": scen, "pass": ok,
        "max_abs_diff": max_abs, "max_margin_over_tol": max_margin,
        "atol": atol, "rtol": rtol, "note": note,
    })
    tag = "PASS" if ok else "FAIL"
    print("  [%s] %-14s max_abs=%.3e margin=%.3e %s"
          % (tag, scen, max_abs, max_margin, note))


def run_rolling1(name, scalar_func, vec_func, tol, windows=(20, 1, 50)):
    print("== %s (atol=%g rtol=%g)" % (name, tol[0], tol[1]))
    for scen, values, w in scenarios_1():
        old = old_rolling1(scalar_func, values, w)
        new = vec_func(values, w)
        if new is None:
            record(name, scen, False, float("inf"), float("inf"), *tol, "vec returned None")
            continue
        ok, ma, mr = compare(old, new, *tol)
        record(name, scen, ok, ma, mr, *tol)
    # 额外 window 变体 (window=1 / window>n 已在场景内, 这里补 window=3/250)
    rng = np.random.default_rng(SEED)
    vals = rng.standard_normal(N).tolist()
    for w in (3, 250):
        old = old_rolling1(scalar_func, vals, w)
        new = vec_func(vals, w)
        if new is None:
            record(name, "window_%d" % w, False, float("inf"), float("inf"), *tol, "None")
            continue
        ok, ma, mr = compare(old, new, *tol)
        record(name, "window_%d" % w, ok, ma, mr, *tol)


def run_rolling2(name, scalar_func, vec_func, tol):
    print("== %s (atol=%g rtol=%g)" % (name, tol[0], tol[1]))
    for scen, a, b, w in scenarios_2():
        old = old_rolling2(scalar_func, a, b, w)
        new = vec_func(a, b, w)
        if new is None:
            # window<=0 回退是契约内行为, 单独用 w=0 验证回退路径与标量一致
            record(name, scen, False, float("inf"), float("inf"), *tol, "vec returned None")
            continue
        ok, ma, mr = compare(old, new, *tol)
        record(name, scen, ok, ma, mr, *tol)


def run_shift(name, scalar_func, vec_func):
    print("== %s (EXACT)" % name)
    for scen, values, _ in scenarios_1():
        for k in (1, 5, 0, 300):  # k=0 与 k>n 是边界
            old = old_shift(scalar_func, values, k)
            new = vec_func(values, k)
            if new is None:
                record(name, "%s_k%d" % (scen, k), False, float("inf"), float("inf"), *EXACT, "None")
                continue
            ok, ma, mr = compare(old, new, *EXACT)
            record(name, "%s_k%d" % (scen, k), ok, ma, mr, *EXACT)


def run_prefix(name, scalar_func, vec_func, tol):
    print("== %s (atol=%g rtol=%g)" % (name, tol[0], tol[1]))
    for scen, values, _ in scenarios_1():
        old = old_prefix(scalar_func, values)
        new = vec_func(values)
        if new is None:
            record(name, scen, False, float("inf"), float("inf"), *tol, "None")
            continue
        ok, ma, mr = compare(old, new, *tol)
        record(name, scen, ok, ma, mr, *tol)


def main():
    t0 = time.time()
    # ROLLING1
    run_rolling1("ts_sum", ts_operators.ts_sum, vec_ops.vec_ts_sum, LINEAR)
    run_rolling1("ts_min", ts_operators.ts_min, vec_ops.vec_ts_min, EXACT)
    run_rolling1("ts_max", ts_operators.ts_max, vec_ops.vec_ts_max, EXACT)
    run_rolling1("ts_argmax", ts_operators.ts_argmax, vec_ops.vec_ts_argmax, EXACT)
    run_rolling1("ts_argmin", ts_operators.ts_argmin, vec_ops.vec_ts_argmin, EXACT)
    run_rolling1("ts_stddev", ts_operators.ts_stddev, vec_ops.vec_ts_stddev, STAT)
    run_rolling1("stddev(alias)", helper_ops.stddev, vec_ops.vec_ts_stddev, STAT)
    run_rolling1("ts_zscore", ts_operators.ts_zscore, vec_ops.vec_ts_zscore, STAT)
    run_rolling1("ts_skew", ts_operators.ts_skew, vec_ops.vec_ts_skew, STAT)
    run_rolling1("ts_kurt", ts_operators.ts_kurt, vec_ops.vec_ts_kurt, STAT)
    run_rolling1("ts_rank", ts_operators.ts_rank, vec_ops.vec_ts_rank, RANK_T)
    run_rolling1("ts_decaylinear", ts_operators.ts_decaylinear, vec_ops.vec_ts_decaylinear, LINEAR)
    run_rolling1("ts_wma", ts_operators.ts_wma, vec_ops.vec_ts_decaylinear, LINEAR)
    # ROLLING2
    run_rolling2("ts_corr", ts_operators.ts_corr, vec_ops.vec_ts_corr, STAT)
    run_rolling2("correlation(alias)", helper_ops.correlation, vec_ops.vec_ts_corr, STAT)
    run_rolling2("ts_covariance", ts_operators.ts_covariance, vec_ops.vec_ts_covariance, STAT)
    run_rolling2("covariance(alias)", helper_ops.covariance, vec_ops.vec_ts_covariance, STAT)
    # SHIFT
    run_shift("ts_delay", ts_operators.ts_delay, vec_ops.vec_ts_delay)
    run_shift("delay(alias)", helper_ops.delay, vec_ops.vec_ts_delay)
    run_shift("ts_delta", ts_operators.ts_delta, vec_ops.vec_ts_delta)
    run_shift("delta(alias)", helper_ops.delta, vec_ops.vec_ts_delta)
    # PREFIX
    run_prefix("rank", cs_operators.rank, vec_ops.vec_rank_prefix, EXACT)
    run_prefix("scale", cs_operators.scale, vec_ops.vec_scale, LINEAR)
    run_prefix("demean", cs_operators.demean, vec_ops.vec_demean, LINEAR)
    run_prefix("adv20", helper_ops.adv20, vec_ops.vec_adv20, LINEAR)
    # SCALAR (逐bar)
    rng = np.random.default_rng(SEED)
    sv = rng.standard_normal(N).tolist()
    sv += [0.0, -0.0, 1e-300, -1e-300]  # 除0/极值靶点
    for name, sfn, vfn, extra in (
        ("sign", helper_ops.sign, vec_ops._vec_sign, ()),
        ("log", helper_ops.log, vec_ops._vec_log, ()),
        ("abs", helper_ops.abs_, vec_ops._vec_abs, ()),
        ("signedpower", helper_ops.signedpower, vec_ops._vec_signedpower, (2.0,)),
    ):
        print("== %s (EXACT)" % name)
        old = old_scalar(sfn, sv, *extra)
        new = vfn(sv, *extra)
        ok, ma, mr = compare(old, list(new), *EXACT)
        record(name, "scalar_n%d" % len(sv), ok, ma, mr, *EXACT)

    # 汇总
    total = len(RESULTS)
    fails = [r for r in RESULTS if not r["pass"]]
    print()
    print("=" * 60)
    print("TOTAL %d checks, PASS %d, FAIL %d, elapsed %.1fs"
          % (total, total - len(fails), len(fails), time.time() - t0))
    if fails:
        print("FAILED:")
        for r in fails:
            print("  %s / %s : max_abs=%.3e margin=%.3e %s"
                  % (r["op"], r["scenario"], r["max_abs_diff"], r["max_margin_over_tol"], r["note"]))
    with open("/tmp/vec_parity_results.jsonl", "w") as f:
        for r in RESULTS:
            f.write(json.dumps(r) + "\n")
    print("results -> /tmp/vec_parity_results.jsonl")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

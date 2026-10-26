"""刀4案2⑦ 二级对拍: 20 个 alpha 端到端 score 一致性 (parser 接线后)。

new = evaluate_series 走 vec_ops 向量化; old = 清空 vec 注册表强制回退
逐 bar 标量循环 (同一 parser 代码路径, 零复制)。同股同日同 AST 比对。

阈值 (docs/alpha-vec-tolerance.md 预注册):
  全序列 atol=1e-8 rtol=1e-6 (np.allclose 组合判据)
  末位值 (下游实际消费的当日因子值) 收紧 atol=1e-9 rtol=1e-9
样本: >=5 只真实股票, date=2026-08-25, spawn 子进程 (生产等效)。
只读生产库 (ro URI), 零生产写。输出落 jsonl 供统筹抽查。
"""
import os
import json
import sys
import time

sys.path.insert(0, "src")

DB = os.path.expanduser("~/qs/data/qs.db")
DATE = "2026-08-25"
N_STOCKS = 6  # >=5

ATOL, RTOL = 1e-8, 1e-6
ATOL_LAST, RTOL_LAST = 1e-9, 1e-9


def _child(codes, conn):
    import numpy as np
    from common.database import Database
    from data_layer import DataLayer
    from strategy_layer.engine import FactorEngine
    from strategy_layer.expression import vec_ops
    from strategy_layer.expression.factor_expression import ALPHA_EXPRESSIONS
    from strategy_layer.expression.parser import evaluate_series, parse, tokenize

    db = Database(DB)
    dl = DataLayer(db)
    engine = FactorEngine(data_layer=dl, mode="live", db=db)
    mw = max(f.window_size for f in engine.factors)
    alpha_factors = [f for f in engine.factors if getattr(f, "category", "") == "alpha"]
    build_ctx_fn = type(alpha_factors[0])._build_context

    asts = {name: parse(tokenize(expr)) for name, expr in ALPHA_EXPRESSIONS.items()}

    # warmup 丢弃
    for c in codes[:2]:
        ctx = engine.build_context(c, DATE, mw)
        for f in engine.factors[:5]:
            try:
                f.compute(ctx)
            except Exception:
                pass

    results = []
    t_old_tot = 0.0
    t_new_tot = 0.0

    # 保存注册表, old 路径清空强制回退标量循环
    saved = (vec_ops.ROLLING1, vec_ops.ROLLING2, vec_ops.SHIFT, vec_ops.PREFIX)

    def disable_vec():
        vec_ops.ROLLING1 = {}
        vec_ops.ROLLING2 = {}
        vec_ops.SHIFT = {}
        vec_ops.PREFIX = {"adv20": lambda v: None}  # adv20 分支直查, 给 None 回退

    def enable_vec():
        (vec_ops.ROLLING1, vec_ops.ROLLING2,
         vec_ops.SHIFT, vec_ops.PREFIX) = saved

    for c in codes[2:2 + N_STOCKS]:
        ctx = engine.build_context(c, DATE, mw)
        context = build_ctx_fn(ctx)
        for name, ast in asts.items():
            # old (标量逐bar)
            disable_vec()
            t0 = time.time()
            try:
                old = evaluate_series(ast, context)
                old_err = None
            except Exception as e:
                old, old_err = None, repr(e)
            t_old_tot += time.time() - t0
            # new (向量化)
            enable_vec()
            t0 = time.time()
            try:
                new = evaluate_series(ast, context)
                new_err = None
            except Exception as e:
                new, new_err = None, repr(e)
            t_new_tot += time.time() - t0

            if old_err or new_err:
                ok = (old_err == new_err)  # 异常行为也必须一致
                results.append({"code": c, "alpha": name, "pass": ok,
                                "old_err": old_err, "new_err": new_err,
                                "note": "exception path"})
                continue

            o = np.asarray(old, dtype=np.float64)
            v = np.asarray(new, dtype=np.float64)
            if len(o) != len(v):
                results.append({"code": c, "alpha": name, "pass": False,
                                "note": "len mismatch %d vs %d" % (len(o), len(v))})
                continue
            nan_o, nan_v = np.isnan(o), np.isnan(v)
            if not np.array_equal(nan_o, nan_v):
                results.append({"code": c, "alpha": name, "pass": False,
                                "note": "NaN pattern mismatch"})
                continue
            fin = ~nan_o
            if fin.any():
                d = np.abs(v[fin] - o[fin])
                tol = ATOL + RTOL * np.abs(o[fin])
                seq_ok = bool(np.all(d <= tol))
                max_abs = float(d.max())
            else:
                seq_ok, max_abs = True, 0.0
            # 末位值收紧 (bool() 包裹: numpy bool_ 不可 json 序列化)
            last_ok = bool(abs(v[-1] - o[-1]) <= ATOL_LAST + RTOL_LAST * abs(o[-1])) \
                if fin.any() else True
            ok = bool(seq_ok and last_ok)
            results.append({"code": c, "alpha": name, "pass": ok,
                            "max_abs_diff": max_abs,
                            "last_old": float(o[-1]), "last_new": float(v[-1]),
                            "last_diff": float(abs(v[-1] - o[-1]))})

    conn.send((results, round(t_old_tot, 3), round(t_new_tot, 3),
               len(codes[2:2 + N_STOCKS]), len(asts)))
    conn.close()


def main():
    import multiprocessing as mp
    from common.database import Database

    db = Database(DB)
    conn = db.get_connection()
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM daily_klines WHERE date=? ORDER BY code",
        (DATE,))][: 2 + N_STOCKS]
    conn.close()

    ctx_mp = mp.get_context("spawn")
    pc, cc = ctx_mp.Pipe()
    p = ctx_mp.Process(target=_child, args=(codes, cc))
    p.start()
    if not pc.poll(600):
        p.kill()
        print("TIMEOUT")
        sys.exit(2)
    results, t_old, t_new, n_stocks, n_alpha = pc.recv()
    p.join()

    total = len(results)
    fails = [r for r in results if not r["pass"]]
    print("二级端到端: %d stocks x %d alphas = %d checks" % (n_stocks, n_alpha, total))
    print("PASS %d, FAIL %d" % (total - len(fails), len(fails)))
    print("耗时: old(标量逐bar)=%.2fs  new(向量化)=%.2fs  speedup=%.1fx"
          % (t_old, t_new, t_old / max(t_new, 1e-9)))
    print("单股单alpha均值: old=%.2fms  new=%.2fms"
          % (t_old / total * 1000, t_new / total * 1000))
    if fails:
        print("FAILED:")
        for r in fails[:20]:
            print("  %s / %s : %s" % (r["code"], r["alpha"],
                  r.get("note", "max_abs=%.3e last_diff=%.3e"
                        % (r.get("max_abs_diff", -1), r.get("last_diff", -1)))))
    with open("/tmp/vec_e2e_results.jsonl", "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    print("results -> /tmp/vec_e2e_results.jsonl")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

"""AUD-021 回归: 漂移哨兵参考截面装载必须 O(参考日), 不是 O(因子×参考日)。

病根 (orchestrator.py 漂移段): `for fname in factor_scores_dict` 内套
`for rd in ref_dates` 内 `SELECT factor_scores FROM factor_daily WHERE date=?`
—— **同一批参考日数据被逐因子重复查询 + 重复 JSON 反序列化**。
量级: 89 因子 x ref_window(默认 10) x 约 5,500 行 = 约 490 万次 JSON 解析
(ref_window=20 时约 980 万次)。

当前 `factor_drift.enabled` 默认 False 整段跳过 = **休眠雷**; 而该开关自述
"通电=纯配置翻转", 一行翻开即引爆 —— 故通电前必修 (统筹 sm-11c299ef 开工令)。

修法: 参考日循环提到因子循环外, 一次装载按因子名分桶。
本测试锁两个契约: ①结果与朴素嵌套算法逐因子等价 ②查询次数 == 参考日数。
"""
import json

import pytest

from common.database import Database
from ops_layer.orchestrator import _load_ref_factor_values


class _CountingConn:
    """只数 SELECT 次数, 其余全透传 —— 把性能属性变成可断言的契约。"""

    def __init__(self, real):
        self._real = real
        self.select_calls = 0

    def execute(self, sql, *a, **kw):
        if sql.lstrip().upper().startswith("SELECT"):
            self.select_calls += 1
        return self._real.execute(sql, *a, **kw)

    def __getattr__(self, item):
        return getattr(self._real, item)


@pytest.fixture
def db():
    d = Database(":memory:")
    conn = d.get_connection()
    conn.execute("CREATE TABLE factor_daily (date TEXT, code TEXT, factor_scores TEXT)")
    conn.commit()
    yield d
    d.close()


def _seed(db, dates, codes, factors):
    conn = db.get_connection()
    for di, date in enumerate(dates):
        for ci, code in enumerate(codes):
            scores = {f: round(0.1 * (di + 1) + 0.01 * (ci + 1) + 0.001 * fi, 4)
                      for fi, f in enumerate(factors)}
            conn.execute(
                "INSERT INTO factor_daily (date, code, factor_scores) VALUES (?, ?, ?)",
                (date, code, json.dumps(scores)))
    conn.commit()


def _naive(conn, ref_dates, factors):
    """朴素嵌套版 (病版语义) —— 作为等价性的黄金参照。"""
    out = {}
    for fname in factors:
        vals = []
        for rd in ref_dates:
            for (fs_json,) in conn.execute(
                    "SELECT factor_scores FROM factor_daily "
                    "WHERE date = ? AND factor_scores IS NOT NULL", (rd,)).fetchall():
                fs = json.loads(fs_json)
                v = fs.get(fname)
                if v is not None:
                    vals.append(float(v))
        out[fname] = vals
    return out


class TestRefLoadingEquivalence:
    def test_matches_naive_nested_result(self, db):
        dates = ["2026-09-01", "2026-09-02", "2026-09-03"]
        codes = ["000001", "000002", "000003", "000004"]
        factors = ["momentum_20d", "std20", "rsi_14"]
        _seed(db, dates, codes, factors)
        conn = db.get_connection()

        fast = _load_ref_factor_values(conn, dates)
        gold = _naive(conn, dates, factors)

        assert set(fast) == set(gold)
        for f in factors:
            assert sorted(fast[f]) == sorted(gold[f]), f"因子 {f} 的参考截面值必须等价"
            assert len(fast[f]) == len(dates) * len(codes)


class TestRefLoadingQueryBudget:
    """性能契约: 查询次数只随参考日数增长, 与因子数无关。"""

    def test_query_count_equals_ref_days_not_factors_times_days(self, db):
        dates = ["2026-09-01", "2026-09-02"]
        codes = [f"{i:06d}" for i in range(5)]
        factors = [f"f{i:02d}" for i in range(12)]      # 12 个因子
        _seed(db, dates, codes, factors)

        counting = _CountingConn(db.get_connection())
        res = _load_ref_factor_values(counting, dates)

        assert len(res) == 12
        assert counting.select_calls == len(dates), (
            "参考截面必须每个参考日只查一次 —— 病版是 因子数 x 参考日数 = "
            f"{len(factors) * len(dates)} 次, 实得 {counting.select_calls} 次")


class TestRefLoadingRobustness:
    def test_bad_json_rows_are_skipped_not_fatal(self, db):
        conn = db.get_connection()
        conn.execute("INSERT INTO factor_daily VALUES ('2026-09-01','000001','{bad json')")
        conn.execute(
            "INSERT INTO factor_daily VALUES ('2026-09-01','000002',?)",
            (json.dumps({"momentum_20d": 1.5}),))
        conn.commit()
        res = _load_ref_factor_values(conn, ["2026-09-01"])
        assert res["momentum_20d"] == [1.5], "坏 JSON 跳过, 好行照收 (与病版容错语义一致)"

    def test_empty_ref_dates_returns_empty(self, db):
        assert _load_ref_factor_values(db.get_connection(), []) == {}

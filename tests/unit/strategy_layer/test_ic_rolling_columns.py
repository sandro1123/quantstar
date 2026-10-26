"""① `record_ic` 补列 (std_ic/icir/win_rate) — 滚动 20 期口径。

F席 E6 问题① 施工 (统筹 0270ea38 三轮裁定, 卡
receipts/2026-09-16_F-E6问题①补列施工卡.md):
- 口径: 滚动 20 期, **含当日** (裁定3)
- 数据源: **DB 查近 20 期** (裁定2 备选; 跨冷启动稳健)
- 窗不足 (<20 期): 三列写 **NULL** (宁缺勿脏; 1~2 期 std≈0 → icir 爆假值)
- N=20 与 `governance.FAST_WINDOW` **同源** (守卫测试)
- 真源引注: `orchestrator/allocator.py:113-114` (`std_ic=np.std(ic 序列)`,
  `icir=mean/std`) —— 非 `ic_feedback.py` (该文件不存在, 原卡勘误)。
  另注 `aggregation/ic_calculator.py:61-62` 的 std_ic 是**横截面**口径
  `std(前向收益)`, **语义不同** —— 防后来者误引。

关键陷阱 (施工前零负载核查发现, 统筹升格为工程判例):
  `INSERT OR REPLACE` 覆盖 `(calc_date, factor_name)` 同日行, 而 `record_ic`
  循环内逐因子 INSERT → 若窗查询含同日, 当日值**双计**。
  修法: `calc_date < :date` 严格排除同日 —— 这同时排除「未来行」
  (record_ic 写 T−horizon, 而库中可能有更晚日期的行)。
"""
import pytest

from common.database import Database
from strategy_layer.feedback.persistence import WeightPersistence

_D = "2026-02-01"


@pytest.fixture
def mem_db():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE factor_ic (
            calc_date TEXT NOT NULL, factor_name TEXT NOT NULL,
            mean_ic REAL, std_ic REAL, icir REAL, win_rate REAL,
            effectiveness TEXT, sample_size INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (calc_date, factor_name)
        )
    """)
    conn.commit()
    return db


def _seed(mem_db, factor, vals, month="01"):
    """直插历史 mean_ic 行 (2026-<month>-01 起逐日)。"""
    conn = mem_db.get_connection()
    for i, v in enumerate(vals):
        conn.execute(
            "INSERT INTO factor_ic (calc_date, factor_name, mean_ic) VALUES (?,?,?)",
            (f"2026-{month}-{i + 1:02d}", factor, float(v)))
    conn.commit()


def _row(mem_db, factor, date=_D):
    return mem_db.get_connection().execute(
        "SELECT mean_ic, std_ic, icir, win_rate FROM factor_ic "
        "WHERE calc_date=? AND factor_name=?", (date, factor)).fetchone()


def _stats(win):
    """总体标准差口径 (与 allocator.py:113-114 同式, ddof=0)。"""
    n = len(win)
    mu = sum(win) / n
    sd = (sum((x - mu) ** 2 for x in win) / n) ** 0.5
    return mu, sd, (mu / sd if sd > 0 else None), sum(1 for x in win if x > 0) / n


# ---- RED: 窗足 → 三列非空且等于手工窗 ----

def test_rolling_columns_match_manual_window(mem_db):
    """RED: 19 期历史 + 当日 = 满 20 期 → 三列非空, == 手工「最近 19 期 + 当日」。"""
    hist = [0.01 * i for i in range(1, 20)]      # 19 期, 互异
    _seed(mem_db, "f1", hist)
    WeightPersistence(db=mem_db).record_ic({"f1": 0.5}, calc_date=_D)
    mean_db, std_db, icir_db, wr_db = _row(mem_db, "f1")
    mu, sd, icir, wr = _stats(hist + [0.5])
    assert mean_db == 0.5
    assert std_db == pytest.approx(sd, abs=1e-12), f"{std_db} != {sd}"
    assert icir_db == pytest.approx(icir, abs=1e-12), f"{icir_db} != {icir}"
    assert wr_db == pytest.approx(wr, abs=1e-12), f"{wr_db} != {wr}"


def test_zero_std_writes_null_icir(mem_db):
    """RED: 窗内全同值 → icir NULL (不写 ±inf/假值)。

    ⚠ 本测试**不断言 std 精确等于 0.0** —— `[0.1]*20` 的 std 在实数域为 0,
    但浮点上**随解释器版本变** (F席 09-16 实测):
        CPython 3.12 (Neumaier 补偿求和) → sum=2.0            → std=0.0
        CPython 3.11 (朴素求和)          → sum=2.0000000000000004
                                          → std=1.3877787807814457e-17
    故断言用 `approx(0.0, abs=1e-12)`: **本测试测的是"icir 是否为 NULL"
    (由 IC_STD_EPS 判据保证), 不是"浮点是否精确"。**
    (判例: 判"某值应精确等于 X"前, 先算一遍"它实际是多少" —— 且跨版本算。)
    """
    _seed(mem_db, "f1", [0.1] * 19)
    WeightPersistence(db=mem_db).record_ic({"f1": 0.1}, calc_date=_D)
    _, std_db, icir_db, wr_db = _row(mem_db, "f1")
    assert std_db == pytest.approx(0.0, abs=1e-12), \
        f"std_ic 应 ≈0 (浮点噪声允许), 实为 {std_db}"
    assert icir_db is None, f"std≈0 时 icir 应为 NULL, 实为 {icir_db}"
    assert wr_db == pytest.approx(1.0, abs=1e-12)


def test_near_constant_series_writes_null_icir(mem_db):
    """RED (V4, 统筹 09-16): **近似常数序列** → icir NULL, 不写天文数字。

    形态②「浮点噪声 std」: 序列近似恒定 (值差 ~1e-16) 时 std 不是 0.0 而是
    ~1e-17 量级 → 若判据为 `std > 0` 则 icir = mean/1e-17 ≈ **1e15 天文数字**
    → 周报按 icir 排序时该因子**永远排第一**, 比"全 NULL"更坏的假信号。
    判据须为 `std > IC_STD_EPS` (1e-12)。
    """
    from strategy_layer.feedback.persistence import IC_STD_EPS
    _seed(mem_db, "f1", [0.05 + 1e-16 * (i % 2) for i in range(19)])
    WeightPersistence(db=mem_db).record_ic({"f1": 0.05}, calc_date=_D)
    _, std_db, icir_db, _ = _row(mem_db, "f1")
    assert std_db is not None and std_db < IC_STD_EPS, \
        f"近似常数序列 std 应 < EPS({IC_STD_EPS}), 实为 {std_db}"
    assert icir_db is None, f"近似常数序列 icir 应为 NULL, 实为 {icir_db} (天文数字?)"
    if icir_db is not None:
        assert abs(icir_db) < 1e6, f"icir 爆天文数字: {icir_db}"


def test_std_just_above_eps_writes_icir(mem_db):
    """对照 (防 EPS 过宽): std 明显 > EPS → icir 照写, 不得误判为 0。"""
    from strategy_layer.feedback.persistence import IC_STD_EPS
    _seed(mem_db, "f1", [0.01 * i for i in range(1, 20)])
    WeightPersistence(db=mem_db).record_ic({"f1": 0.5}, calc_date=_D)
    _, std_db, icir_db, _ = _row(mem_db, "f1")
    assert std_db > IC_STD_EPS, f"该序列 std 应 > EPS, 实为 {std_db}"
    assert icir_db is not None, "std 明显大于 EPS 时 icir 不应为 NULL (EPS 过宽)"


def test_same_day_rewrite_not_double_counted(mem_db):
    """RED: 同因子同日改写 → 当日只占窗内 1 席 (新值), 非叠加。

    机理: `INSERT OR REPLACE` 覆盖同日行; 若窗查询未排除同日, 则
    「查出的历史已含当日旧值」+「本轮新值」→ 双计 → std 偏。
    """
    _seed(mem_db, "f1", [0.1] * 19)
    wp = WeightPersistence(db=mem_db)
    wp.record_ic({"f1": 0.5}, calc_date=_D)
    wp.record_ic({"f1": 0.9}, calc_date=_D)       # 同日改写
    _, std_db, _, wr_db = _row(mem_db, "f1")
    _, sd, _, wr = _stats([0.1] * 19 + [0.9])
    assert std_db == pytest.approx(sd, abs=1e-12), \
        f"同日改写应仅以新值入窗 (非双计): {std_db} != {sd}"
    assert wr_db == pytest.approx(wr, abs=1e-12)


def test_future_dated_rows_excluded_from_window(mem_db):
    """RED: 库中「比 calc_date 更晚」的行不得入窗 (防假绿)。

    现实场景 (`test_ic_evidence_guard._seed_factor_ic_history`): 铺 today−1..today−3,
    而 `record_ic` 写 T−horizon → 若窗查询不过滤会混入未来行。
    """
    _seed(mem_db, "f1", [0.1] * 19)
    conn = mem_db.get_connection()
    conn.execute("INSERT INTO factor_ic (calc_date, factor_name, mean_ic) VALUES (?,?,?)",
                 ("2026-03-01", "f1", 9.9))       # 未来行
    conn.commit()
    WeightPersistence(db=mem_db).record_ic({"f1": 0.5}, calc_date=_D)
    _, std_db, _, _ = _row(mem_db, "f1")
    _, sd, _, _ = _stats([0.1] * 19 + [0.5])
    assert std_db == pytest.approx(sd, abs=1e-12), \
        f"未来行不得入窗: {std_db} != {sd}"


# ---- 守卫: 锁行为 (当前实现即通过, 防未来退化) ----

def test_insufficient_window_writes_null_but_mean_ic(mem_db):
    """守卫: 窗 < 20 期 → 三列 NULL, `mean_ic` 照写 (宁缺勿脏, 不写假值)。"""
    _seed(mem_db, "f1", [0.1] * 5)
    WeightPersistence(db=mem_db).record_ic({"f1": 0.5}, calc_date=_D)
    mean_db, std_db, icir_db, wr_db = _row(mem_db, "f1")
    assert mean_db == 0.5, "mean_ic 须照写 (与三列独立)"
    assert std_db is None and icir_db is None and wr_db is None, \
        f"窗不足应写 NULL, 实为 ({std_db}, {icir_db}, {wr_db})"


def test_rolling_window_same_as_governance_fast_window():
    """守卫: 滚动窗长与 `governance.FAST_WINDOW` 同源 (单一定义点, 防漂移)。"""
    from strategy_layer.feedback import persistence as P
    from strategy_layer.governance import FAST_WINDOW
    assert P.IC_ROLLING_WINDOW == FAST_WINDOW, \
        f"persistence.IC_ROLLING_WINDOW={P.IC_ROLLING_WINDOW} 与 " \
        f"governance.FAST_WINDOW={FAST_WINDOW} 漂移"

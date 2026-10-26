"""fwd_10d: 10d 前向收益 — 单次遍历四周期 + 缺列防御 + 无 DEFAULT 钉死

:memory: 库, 无生产写。验证:
- backfill_fwd_returns 默认四周期 (1/5/10/20), 一次扫表全算
- 10d 滞后 = 10 个交易日 (非自然日, F-37 负向对照)
- 缺列防御: fwd_return_10d 列不存在 → 该 horizon 跳过留痕 (-1), 管道照常
- DDL 脚本: dry-run 零写入; --apply 后 dflt_value 必须为 NULL
- 单一事实源: horizon_map 字面量禁止再复制 (静态扫描)
"""
import ast
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_connection(self):
        return self.conn


def _mk_db(with_10d=True):
    db = _FakeDB()
    db.conn.execute(
        "CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, "
        "PRIMARY KEY (code, date))")
    cols = ("fwd_return_1d REAL, fwd_return_3d REAL, fwd_return_5d REAL, "
            + ("fwd_return_10d REAL, " if with_10d else "")
            + "fwd_return_20d REAL")
    db.conn.execute(
        f"CREATE TABLE factor_daily (date TEXT, code TEXT, {cols}, "
        "PRIMARY KEY (date, code))")
    db.conn.commit()
    return db


def _loader(db):
    from data_layer._loader import DataLoader
    return DataLoader(db)


def _add_kline(db, code, date, close):
    db.conn.execute(
        "INSERT INTO daily_klines VALUES (?, ?, ?)", (code, date, close))


def _add_factor_row(db, date):
    db.conn.execute(
        "INSERT INTO factor_daily (date, code) VALUES (?, '000001')", (date,))
    db.conn.commit()


# ---------- 四周期默认 + 交易日语义 ----------

def test_backfill_defaults_include_10d():
    from data_layer._loader import FWD_HORIZONS
    assert FWD_HORIZONS == (1, 3, 5, 10, 20)


def test_backfill_10d_trading_day_lag_not_natural():
    """F-37 负向对照: 10 个交易日后兑现, 中间夹周末也不得用自然日。

    构造: 2026-08-03(周一) 起 15 个连续"日历日"(含周末), 全部当交易日。
    若按自然日 +10 会落到 08-13 的 close; 按交易日第 10 个是 08-13。
    再造一个跳空场景区分两者: 让 close 序列单调递增 1..15,
    交易日第 10 个 close=14 (08-03 后第 10 行), 自然日第 10 天也是同序列——
    因此改用缺口日期表: 08-08/09 不在 kline 表 (周末休市)。
    """
    db = _mk_db()
    ld = _loader(db)
    # 08-03 周一起, 跳过周末 08-08/09, 共 12 个交易日
    trading_days = ["2026-08-%02d" % d for d in (3, 4, 5, 6, 7, 10, 11, 12, 13,
                                                 14, 17, 18)]
    for i, d in enumerate(trading_days):
        _add_kline(db, "000001", d, 100.0 + i)
    for d in trading_days[:11]:
        _add_factor_row(db, d)

    res = ld.backfill_fwd_returns("2026-08-07", horizons=(10,))
    # 08-07 是 idx3; idx3+10=idx13 超出表尾 → 未兑现: 不写入, 计数 0, 保持 NULL
    assert res[10] == 0
    row = db.conn.execute(
        "SELECT fwd_return_10d FROM factor_daily WHERE date='2026-08-07'"
    ).fetchone()
    assert row["fwd_return_10d"] is None

    # 兑现案例: 08-03 (idx0) 的 10 日后 = trading_days[10] = 08-17, close=110
    res = ld.backfill_fwd_returns("2026-08-03", horizons=(10,))
    assert res[10] == 1
    row = db.conn.execute(
        "SELECT fwd_return_10d FROM factor_daily WHERE date='2026-08-03'"
    ).fetchone()
    expected = (110.0 - 100.0) / 100.0 * 100  # 百分点
    assert abs(row["fwd_return_10d"] - expected) < 1e-9
    # 自然日语义会取 2026-08-13 (close=108) → 与上面断言互斥


def test_backfill_all_four_horizons_one_pass():
    """统筹约束: 同一次调用同时算 1/5/10/20, 不是为 10d 单开路径。"""
    db = _mk_db()
    ld = _loader(db)
    days = ["2026-09-%02d" % d for d in range(1, 26)]  # 25 连续日当交易日
    for i, d in enumerate(days):
        _add_kline(db, "000001", d, 50.0 + i)
    for d in days[:21]:
        _add_factor_row(db, d)

    res = ld.backfill_fwd_returns("2026-09-01")  # 默认 horizons
    assert set(res.keys()) == {1, 3, 5, 10, 20}
    assert all(v >= 0 for v in res.values())
    row = db.conn.execute(
        "SELECT fwd_return_1d, fwd_return_5d, fwd_return_10d, fwd_return_20d "
        "FROM factor_daily WHERE date='2026-09-01'").fetchone()
    assert row["fwd_return_1d"] is not None
    assert row["fwd_return_5d"] is not None
    assert row["fwd_return_10d"] is not None
    assert row["fwd_return_20d"] is not None


# ---------- 缺列防御 ----------

def test_backfill_skips_missing_column_without_killing_pipeline():
    """写好待 GO 关键: 10d 列未建期间, 1d/5d/20d 照常跑, 10d 记 -1 不炸不静默。"""
    db = _mk_db(with_10d=False)
    ld = _loader(db)
    days = ["2026-09-%02d" % d for d in range(1, 26)]
    for i, d in enumerate(days):
        _add_kline(db, "000001", d, 50.0 + i)
    for d in days[:21]:
        _add_factor_row(db, d)

    res = ld.backfill_fwd_returns("2026-09-01")
    assert res[10] == -1          # 约定: -1 = 缺列被跳过 (调用方记 errors)
    assert res[1] == 1 and res[5] == 1 and res[20] == 1  # 其余照常
    row = db.conn.execute(
        "SELECT fwd_return_1d FROM factor_daily WHERE date='2026-09-01'").fetchone()
    assert row["fwd_return_1d"] is not None


# ---------- DDL 脚本 ----------

def _tmp_db_file(tmp_path):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE factor_daily (date TEXT, code TEXT, "
        "fwd_return_1d REAL, fwd_return_3d REAL, fwd_return_5d REAL, fwd_return_20d REAL, "
        "PRIMARY KEY (date, code))")
    conn.commit()
    conn.close()
    return str(p)


def test_ddl_dry_run_zero_write(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ddl", os.path.join(PROJECT_ROOT, "scripts",
                            "add_fwd_return_10d_column.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    dbp = _tmp_db_file(tmp_path)
    rc = mod.main.__wrapped__ if hasattr(mod.main, "__wrapped__") else None
    sys.argv = ["x", "--db", dbp]
    assert mod.main() == 0
    conn = sqlite3.connect(dbp)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(factor_daily)")]
    conn.close()
    assert "fwd_return_10d" not in cols  # dry-run 未建列


def test_ddl_apply_no_default_clause(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ddl", os.path.join(PROJECT_ROOT, "scripts",
                            "add_fwd_return_10d_column.py"))
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.chdir(PROJECT_ROOT)
    spec.loader.exec_module(mod)

    dbp = _tmp_db_file(tmp_path)
    sys.argv = ["x", "--db", dbp, "--apply"]
    assert mod.main() == 0
    conn = sqlite3.connect(dbp)
    meta = {r[1]: r[4] for r in conn.execute("PRAGMA table_info(factor_daily)")}
    conn.close()
    assert "fwd_return_10d" in meta
    assert meta["fwd_return_10d"] is None  # 统筹钉死: 新列不带 DEFAULT


# ---------- 单一事实源 (断裂五形第五形防复发) ----------

def test_no_horizon_map_literal_duplication():
    """horizon->列名映射只允许出现在 data_layer/_loader.py 常量定义处。"""
    offenders = []
    for root, dirs, files in os.walk(os.path.join(PROJECT_ROOT, "src")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            with open(path, encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read())
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    keys = [k.value if isinstance(k, ast.Constant) else None
                            for k in node.keys]
                    if keys == [1, 5, 20] or keys == [1, 5, 10, 20]:
                        offenders.append(path.replace(PROJECT_ROOT + os.sep, ""))
    allowed = {"src" + os.sep + "data_layer" + os.sep + "_loader.py"}
    bad = [o for o in offenders if o not in allowed]
    assert not bad, f"horizon_map 字面量复制于: {bad}"

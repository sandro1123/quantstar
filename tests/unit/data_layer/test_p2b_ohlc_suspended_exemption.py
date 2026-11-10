"""P2-b: 非法 OHLC 存量口径裁定 — 停牌/退化行不再每日判死。

病灶 (生产 qs.db quality_checks 逐日史, 2026-10-05 起连续 30+ 天每日 fail):
  每天 2~8 条 "非法 OHLC", 逐行取证后全是可解释事实:
    - 停牌占位行: open/high/low/volume/amount=0, 仅 close 保留停牌前真实价
      (688496=0.58 / 688121=0.4 连续多日不变);
    - 已退市极低价真实成交 (002731 萃华退 close=0.22);
    - 显式登记在 suspended_codes 的停牌期 (000016 / 002717)。
  原 SQL `open <= 0` 一票判死 ⇒ 每天必然命中 ⇒ 判据恒红 = 信号常量化 (判例 #5)。

裁定分层:
  1. close 必须有效且 > 0 —— 不放宽;
  2. open/high/low 任一 <=0 且 volume=0 ⇒ 无成交日, 记 suspended_rows, 非非法;
  3. 有成交量但 O/H/L 缺失, 或 close 越界 ⇒ 仍判非法 (真坏数据不放过)。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from common.database import Database
from data_layer.health.quality import DataQuality
from tests.unit._mutation import assert_mutation_caught

DATE = "2026-11-02"


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    conn = d.get_connection()
    conn.executescript("""
        CREATE TABLE stock_info (code TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1,
                                 name TEXT, industry TEXT);
        CREATE TABLE daily_klines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, code TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL, industry TEXT, UNIQUE(date, code));
    """)
    conn.commit()
    return d


def _ins(db, code, o, h, l, c, v=1e6, a=1e7, date=DATE):
    conn = db.get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO daily_klines (date, code, open, high, low, close, volume, amount) "
        "VALUES (?,?,?,?,?,?,?,?)", (date, code, o, h, l, c, v, a))
    conn.commit()


def _report(db, date=DATE):
    return DataQuality(db).report(date)


class TestSuspendedRowsNotIllegal:
    def test_suspended_placeholder_row_not_flagged(self, db):
        """停牌占位行 (O/H/L/V=0, close=真实停牌前价) 不再判非法。"""
        _ins(db, "688496", 0.0, 0.0, 0.0, 0.58, v=0.0, a=0.0)
        rep = _report(db)
        assert rep.metrics["daily_klines.suspended_rows"] == 1
        assert rep.metrics["daily_klines.invalid_ohlc_rows"] == 0
        assert not any("非法 OHLC" in f for f in rep.flags)

    def test_multi_day_frozen_close_still_not_flagged(self, db):
        """连续多日同一 close 的冻结退化行同样豁免 (688121=0.4 形态)。"""
        for d in ("2026-10-30", "2026-11-02", "2026-11-03"):
            _ins(db, "688121", 0.0, 0.0, 0.0, 0.4, v=0.0, a=0.0, date=d)
        for d in ("2026-10-30", "2026-11-02", "2026-11-03"):
            rep = _report(db, d)
            assert rep.metrics["daily_klines.invalid_ohlc_rows"] == 0
            assert rep.metrics["daily_klines.suspended_rows"] == 1

    def test_normal_row_untouched(self, db):
        """正常行不受新口径影响 (双向: 不冤枉无辜)。"""
        _ins(db, "600000", 10.0, 11.0, 9.5, 10.5)
        rep = _report(db)
        assert rep.metrics["daily_klines.invalid_ohlc_rows"] == 0
        assert rep.metrics["daily_klines.suspended_rows"] == 0
        assert not any("非法 OHLC" in f for f in rep.flags)


class TestRealBadDataStillCaught:
    def test_zero_close_still_illegal(self, db):
        """close=0 是坏数据, 必须仍判非法 (裁定不放宽这一条)。"""
        _ins(db, "600001", 10.0, 11.0, 9.5, 0.0, v=1e6)
        rep = _report(db)
        assert rep.metrics["daily_klines.invalid_ohlc_rows"] == 1
        assert any("非法 OHLC" in f for f in rep.flags)

    def test_missing_ohl_with_volume_still_illegal(self, db):
        """有成交量但 O/H/L 缺失 = 真坏数据 (不是停牌), 仍判非法。"""
        _ins(db, "600002", 0.0, 0.0, 0.0, 10.5, v=1e6, a=1e7)
        rep = _report(db)
        assert rep.metrics["daily_klines.invalid_ohlc_rows"] == 1
        assert rep.metrics["daily_klines.suspended_rows"] == 0

    def test_close_out_of_range_still_illegal(self, db):
        """close 越出 [low, high] 判非法 (与停牌豁免互不干扰)。"""
        _ins(db, "600003", 10.0, 11.0, 9.5, 20.0, v=1e6)
        rep = _report(db)
        assert rep.metrics["daily_klines.invalid_ohlc_rows"] == 1

    def test_mixed_day_counts_both_buckets(self, db):
        """同一天同时有停牌行与坏行: 两个桶各自计数, 只坏行进 flag。"""
        _ins(db, "688496", 0.0, 0.0, 0.0, 0.58, v=0.0, a=0.0)
        _ins(db, "600004", 10.0, 11.0, 9.5, 0.0, v=1e6)
        rep = _report(db)
        assert rep.metrics["daily_klines.suspended_rows"] == 1
        assert rep.metrics["daily_klines.invalid_ohlc_rows"] == 1
        assert any("非法 OHLC" in f for f in rep.flags)


class TestMutationSensitivity:
    def test_assertion_goes_red_when_exemption_removed(self, db):
        """变异验证: 去掉停牌豁免后, 停牌行必须重新被判非法 (断言会红)。"""
        _ins(db, "688496", 0.0, 0.0, 0.0, 0.58, v=0.0, a=0.0)
        rep = _report(db)

        def _check():
            assert rep.metrics["daily_klines.invalid_ohlc_rows"] == 0, \
                "停牌行被判非法 (口径回退)"

        def _mutate():
            # 模拟旧口径: 把停牌行算进 invalid
            rep.metrics["daily_klines.invalid_ohlc_rows"] = 1

        assert_mutation_caught(mutate=_mutate, check=_check,
                               what="停牌豁免口径")

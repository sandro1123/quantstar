"""P-DATA 验收门禁 — 数据完整/规范/有效/真实 的四项规范守护测试"""
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

DB_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "qs.db"

# P0-2: 需真实生产库数据体量, 默认 skip (用 --prod-db 或 -m needs_production_db 启用)
pytestmark = pytest.mark.needs_production_db


@pytest.fixture(scope="module")
def conn():
    if not DB_PATH.exists():
        pytest.skip(f"database not found: {DB_PATH}")
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    yield c
    c.close()


class TestMarketCanonical:
    def test_market_only_sh_sz_bj(self, conn):
        rows = conn.execute("SELECT DISTINCT market FROM stock_info").fetchall()
        bad = [r[0] for r in rows if r[0] not in ("SH", "SZ", "BJ")]
        assert not bad, f"非规范 market 值: {bad}"

    def test_market_matches_code_prefix(self, conn):
        # 6*=SH, 0*/3*=SZ, 4*/8*/9*=BJ
        bad = conn.execute("""
            SELECT code, market FROM stock_info
            WHERE (market='SH' AND NOT code LIKE '6%')
               OR (market='SZ' AND NOT (code LIKE '0%' OR code LIKE '3%'))
               OR (market='BJ' AND NOT (code LIKE '4%' OR code LIKE '8%' OR code LIKE '9%'))
        """).fetchall()
        assert not bad, f"market 与 code 前缀不一致: {bad[:10]}"


class TestDateCanonical:
    def test_valuation_dates_iso(self, conn):
        rows = conn.execute("SELECT DISTINCT date FROM stock_valuation").fetchall()
        bad = [d for (d,) in rows
               if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(d))]
        assert not bad, f"非 ISO 估值日期: {bad[:10]}"

    def test_kline_dates_iso(self, conn):
        rows = conn.execute(
            "SELECT DISTINCT substr(date,1,6) FROM daily_klines LIMIT 100"
        ).fetchall()
        assert rows, "daily_klines 无数据"
        # 抽样校验格式
        raw = conn.execute(
            "SELECT date FROM daily_klines ORDER BY date DESC LIMIT 100"
        ).fetchall()
        bad = [d for (d,) in raw
               if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(d))]
        assert not bad, f"非 ISO K 线日期: {bad[:5]}"


class TestChangePctPlausibility:
    def test_pre_close_mostly_populated(self, conn):
        total = conn.execute("SELECT COUNT(*) FROM daily_klines").fetchone()[0]
        bad = conn.execute(
            "SELECT COUNT(*) FROM daily_klines WHERE pre_close=0 AND volume>0"
        ).fetchone()[0]
        # 允许次日首日/复牌缺口 (~0.05%)
        assert bad / total < 0.01, f"pre_close=0 比例过高: {bad}/{total}"

    def test_change_pct_reasonable_for_normal_days(self, conn):
        # 非ST股票, 排除首日/重组, 超10.5% 应属罕见(除权/重组)
        n = conn.execute("""
            SELECT COUNT(*) FROM daily_klines d JOIN stock_info s ON d.code=s.code
            WHERE s.name NOT LIKE 'ST%' AND s.name NOT LIKE '*ST%'
              AND ABS(d.change_pct) > 10.5 AND d.date > '2010-06-01'
              AND d.pre_close > 0
        """).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM daily_klines d JOIN stock_info s ON d.code=s.code "
            "WHERE s.market='SH' OR s.market='SZ'"
        ).fetchone()[0]
        # 主板10%涨停, 重组/复牌为少数, 占比应 < 2%
        assert n / total < 0.02, f"change_pct 异常比例过高: {n}/{total}"


class TestStFilter:
    def test_st_stocks_detectable_by_name(self, conn):
        # 至少应能识别出一些 ST 股(名称以ST开头)
        st_count = conn.execute(
            "SELECT COUNT(*) FROM stock_info WHERE name LIKE 'ST%'"
        ).fetchone()[0]
        assert st_count > 0, "库中应有 ST 股票"
"""策略A 热度判据测试 — 每路信号正/负/数据缺失三态 + 编排规则 (铁律 #18 负向对照)"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from strategy_layer.lifecycle.heat import (
    assess_heat, lhb_frequency, limit_pool_presence,
    pct_rank, sector_outflow, stock_outflow_streak, volume_decay,
    REC_HOLD, REC_EXIT_REVIEW, REC_LEAN_EXIT_REVIEW, REC_LEAN_HOLD_REVIEW,
    REC_INSUFFICIENT,
)


class _DBAdapter:
    """信号函数只依赖 db.get_connection(); 适配裸连接并透传种子写入。"""

    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn

    def execute(self, *a, **k):
        return self._conn.execute(*a, **k)

    def commit(self):
        return self._conn.commit()


@pytest.fixture()
def db(tmp_path):
    """会话内最小 schema tmp 文件库 (不触生产)。"""
    conn = sqlite3.connect(str(tmp_path / "heat_test.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT);
        CREATE TABLE sector_fund_flow (date TEXT, sector_name TEXT, sector_type TEXT,
            change_pct REAL, main_net_amount REAL, main_net_ratio REAL, source TEXT);
        CREATE TABLE stock_moneyflow (code TEXT, date TEXT, main_net_inflow REAL,
            retail_net_inflow REAL, main_inflow REAL, main_outflow REAL);
        CREATE TABLE daily_klines (code TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, amount REAL, pre_close REAL, change_pct REAL,
            adjust_type TEXT);
        CREATE TABLE stock_limit_up_down (code TEXT, date TEXT, name TEXT, limit_type TEXT,
            price REAL, change_pct REAL, count INTEGER);
        CREATE TABLE limit_up_ladder (snapshot_date TEXT, trade_date TEXT, board_level TEXT,
            code TEXT, name TEXT, board_num INTEGER, seal_nextday INTEGER,
            sign_level INTEGER, source TEXT);
        CREATE TABLE lhb_daily (code TEXT, date TEXT, net_buy_amount REAL,
            drama_score REAL, net_buy_amount_change REAL, drama_score_change REAL,
            total_buy REAL, total_sell REAL);
    """)
    yield _DBAdapter(conn)
    conn.close()


class TestPctRank:
    def test_basic(self):
        assert pct_rank([1.0, 2.0, 3.0, 4.0], 3.5) == 0.75

    def test_empty_returns_none(self):
        assert pct_rank([], 1.0) is None


class TestSectorOutflow:
    def _seed(self, db):
        db.execute("INSERT INTO stock_info VALUES ('300001','X','黄金概念')")
        # 10 个行业: 300001 所在行业净额排最差 20%
        for i in range(8):
            db.execute("INSERT INTO sector_fund_flow VALUES ('2026-08-21',?,'industry',1.0,?,NULL,'akshare')",
                       (f"行业{i}", (i + 1) * 1e7))
        db.execute("INSERT INTO sector_fund_flow VALUES ('2026-08-21','黄金概念','industry',-1.0,-5e7,NULL,'akshare')")
        db.execute("INSERT INTO sector_fund_flow VALUES ('2026-08-21','另一行业','industry',-2.0,-9e7,NULL,'akshare')")
        db.commit()

    def test_fired_when_bottom_and_negative(self, db):
        self._seed(db)
        r = sector_outflow(db, "300001", "2026-08-21")
        assert r.data_ok and r.fired and r.value is not None and r.value <= 0.30

    def test_not_fired_when_positive_inflow(self, db):
        self._seed(db)
        db.execute("UPDATE sector_fund_flow SET main_net_amount=5e8 WHERE sector_name='黄金概念'")
        db.commit()
        r = sector_outflow(db, "300001", "2026-08-21")
        assert r.fired is False

    def test_not_fired_when_negative_but_high_rank(self, db):
        """负向对照: 净额为负但排名不靠后 → 不触发。
        追加 4 个更深负值行业后, 黄金概念(-5e6) 的分位 = 5/15 ≈ 0.33 > 0.30。"""
        self._seed(db)
        for name, amt in [("负A", -8e7), ("负B", -7e7), ("负C", -6e7), ("负D", -5.5e7)]:
            db.execute("INSERT INTO sector_fund_flow VALUES ('2026-08-21',?,'industry',-1.0,?,NULL,'akshare')",
                       (name, amt))
        db.execute("UPDATE sector_fund_flow SET main_net_amount=-5e6 WHERE sector_name='黄金概念'")
        db.commit()
        r = sector_outflow(db, "300001", "2026-08-21")
        assert r.fired is False
        assert r.value > 0.30

    def test_no_industry_mapping_data_not_ok(self, db):
        r = sector_outflow(db, "999999", "2026-08-21")
        assert r.data_ok is False and r.fired is False

    def test_no_sector_flow_that_day(self, db):
        db.execute("INSERT INTO stock_info VALUES ('300001','X','黄金概念')")
        db.commit()
        r = sector_outflow(db, "300001", "2026-08-22")
        assert r.data_ok is False


class TestStockOutflowStreak:
    def test_three_consecutive_outflows_fire(self, db):
        for d, v in [("08-19", -1e7), ("08-20", -2e7), ("08-21", -3e7)]:
            db.execute("INSERT INTO stock_moneyflow VALUES ('300001',?, ?,0,0,0)",
                       (f"2026-{d}", v))
        db.commit()
        r = stock_outflow_streak(db, "300001", "2026-08-21")
        assert r.value == 3 and r.fired

    def test_inflow_resets_streak(self, db):
        """负向对照: 中途一天流入 → 连续性清零。"""
        for d, v in [("08-18", -1e7), ("08-19", +5e6), ("08-20", -2e7), ("08-21", -3e7)]:
            db.execute("INSERT INTO stock_moneyflow VALUES ('300001',?, ?,0,0,0)",
                       (f"2026-{d}", v))
        db.commit()
        r = stock_outflow_streak(db, "300001", "2026-08-21")
        assert r.value == 2 and not r.fired

    def test_no_rows_data_not_ok(self, db):
        r = stock_outflow_streak(db, "300001", "2026-08-21")
        assert r.data_ok is False and r.fired is False


class TestVolumeDecay:
    def test_shrinking_volume_fires(self, db):
        for i in range(5):  # 近 5 日均量 1e8
            db.execute("INSERT INTO daily_klines (code,date,volume) VALUES ('300001',?,?)",
                       (f"2026-08-{14+i}", 1e8))
        db.execute("INSERT INTO daily_klines (code,date,volume) VALUES ('300001','2026-08-21',5e7)")
        db.commit()
        r = volume_decay(db, "300001", "2026-08-21")
        assert r.value == pytest.approx(0.5) and r.fired

    def test_healthy_volume_not_fired(self, db):
        """负向对照: 放量 → 不触发。"""
        for i in range(5):  # 基准 5e7
            db.execute("INSERT INTO daily_klines (code,date,volume) VALUES ('300001',?,5e7)",
                       (f"2026-08-{14+i}",))
        db.execute("INSERT INTO daily_klines (code,date,volume) VALUES ('300001','2026-08-21',1e8)")
        db.commit()
        r = volume_decay(db, "300001", "2026-08-21")
        assert r.fired is False

    def test_insufficient_klines_data_not_ok(self, db):
        for i in range(3):
            db.execute("INSERT INTO daily_klines (code,date,volume) VALUES ('300001',?,1e8)",
                       (f"2026-08-{18+i}",))
        db.commit()
        db.commit()
        r = volume_decay(db, "300001", "2026-08-21")
        assert r.data_ok is False and r.fired is False


class TestLimitPoolPresence:
    def test_in_limit_up_pool_fires(self, db):
        db.execute("INSERT INTO stock_limit_up_down VALUES ('300001','2026-08-21','X','limit_up',10.0,10.0,1)")
        db.commit()
        r = limit_pool_presence(db, "300001", "2026-08-21")
        assert r.fired and r.detail["limit_up_pool"]

    def test_in_ladder_fires(self, db):
        db.execute("INSERT INTO limit_up_ladder VALUES ('2026-08-21','2026-08-21','two_board','300001','X',2,NULL,0,'fuyao')")
        db.commit()
        r = limit_pool_presence(db, "300001", "2026-08-21")
        assert r.fired and r.detail["ladder"]

    def test_absent_but_tables_covered_not_fired_and_ok(self, db):
        """负向对照: 当日表有数据但该股不在 → fired=False 且 data_ok=True (真利空)。"""
        db.execute("INSERT INTO stock_limit_up_down VALUES ('000002','2026-08-21','Y','limit_up',10.0,10.0,1)")
        db.commit()
        r = limit_pool_presence(db, "300001", "2026-08-21")
        assert r.fired is False and r.data_ok is True

    def test_both_tables_empty_data_not_ok(self, db):
        """铁律 #18 关键反例: 空表不能当利空证据。"""
        r = limit_pool_presence(db, "300001", "2026-08-21")
        assert r.data_ok is False and r.fired is False


class TestLhbFrequency:
    def test_on_list_fires(self, db):
        db.execute("INSERT INTO lhb_daily VALUES ('300001','2026-08-21',1e7,50,0,0,2e7,1e7)")
        db.execute("INSERT INTO lhb_daily VALUES ('000002','2026-08-21',1e7,50,0,0,2e7,1e7)")
        db.commit()
        r = lhb_frequency(db, "300001", "2026-08-21")
        assert r.value == 1 and r.fired

    def test_not_on_list_not_fired_but_ok(self, db):
        """负向对照: 榜单有数据、该股不在 → fired=False, data_ok=True。"""
        db.execute("INSERT INTO lhb_daily VALUES ('000002','2026-08-21',1e7,50,0,0,2e7,1e7)")
        db.commit()
        r = lhb_frequency(db, "300001", "2026-08-21")
        assert r.fired is False and r.data_ok is True

    def test_empty_lhb_data_not_ok(self, db):
        r = lhb_frequency(db, "300001", "2026-08-21")
        assert r.data_ok is False

    def test_window_limits_dates(self, db):
        """窗口 = lhb_daily 最近 N 个有数据的日期; 更早日期的上榜不计入。
        库里只有两个有榜日 (07-01, 08-21) 且 window=5 → 两天都进窗口,
        该股在 07-01 上过榜 → 计数 1。"""
        db.execute("INSERT INTO lhb_daily VALUES ('300001','2026-07-01',1e7,50,0,0,2e7,1e7)")
        db.execute("INSERT INTO lhb_daily VALUES ('000002','2026-08-21',1e7,50,0,0,2e7,1e7)")
        db.commit()
        r = lhb_frequency(db, "300001", "2026-08-21", window=5)
        assert r.value == 1 and r.fired is True

    def test_old_listing_outside_date_window_not_counted(self, db):
        """真窗口裁剪: 榜单日期充足时, 只统计最近 window 个有榜日的上榜。"""
        # 造 6 个有榜日 (window=5 只取最近的 5 个: 08-13..08-21)
        for d in ("08-10", "08-13", "08-17", "08-18", "08-19", "08-20", "08-21"):
            db.execute("INSERT INTO lhb_daily VALUES ('000002',?,1e7,50,0,0,2e7,1e7)",
                       (f"2026-{d}",))
        # 该股只在 08-10 (窗口外) 上榜
        db.execute("INSERT INTO lhb_daily VALUES ('300001','2026-08-10',1e7,50,0,0,2e7,1e7)")
        db.commit()
        r = lhb_frequency(db, "300001", "2026-08-21", window=5)
        assert r.value == 0 and r.fired is False
        assert "2026-08-10" not in r.detail["window_dates"]


class TestAssessHeatOrchestration:
    """编排规则: 不同信号组合导向不同处置 (铁律 #19)。"""

    def _seed_all_support(self, db):
        """热度仍在的世界: 行业净流入、个股流入、量能健康、在涨停池、上龙虎榜。
        kline 种子: 近5日均2e8 + 当日(08-21)放量4e8 → volume_decay 不触发。"""
        db.execute("INSERT INTO stock_info VALUES ('300001','X','黄金概念')")
        db.execute("INSERT INTO sector_fund_flow VALUES ('2026-08-21','黄金概念','industry',2.0,5e8,NULL,'akshare')")
        for d in ("08-19", "08-20", "08-21"):
            db.execute("INSERT INTO stock_moneyflow VALUES ('300001',?,?,0,0,0)", (f"2026-{d}", 1e7))
        for i in range(5):  # 基准 5 日: 08-14..08-18
            db.execute("INSERT INTO daily_klines (code,date,volume) VALUES ('300001',?,2e8)",
                       (f"2026-08-{14+i}",))
        db.execute("INSERT INTO daily_klines (code,date,volume) VALUES ('300001','2026-08-21',4e8)")
        db.execute("INSERT INTO stock_limit_up_down VALUES ('300001','2026-08-21','X','limit_up',10.0,10.0,1)")
        db.execute("INSERT INTO lhb_daily VALUES ('300001','2026-08-21',1e7,50,0,0,2e7,1e7)")
        db.commit()

    def _flip_to_decay(self, db):
        """从 support 世界反转为三路衰减: 行业尾部流出/连续流出/缩量; 移出支撑事件。"""
        # 行业净额转负且垫底
        for i in range(8):
            db.execute("INSERT INTO sector_fund_flow VALUES ('2026-08-21',?,'industry',1.0,?,NULL,'akshare')",
                       (f"行{i}", (i + 2) * 1e9))
        db.execute("UPDATE sector_fund_flow SET main_net_amount=-9e8 WHERE sector_name='黄金概念'")
        # 个股连续 3 日流出
        for d in ("08-19", "08-20", "08-21"):
            db.execute("UPDATE stock_moneyflow SET main_net_inflow=-1e7 WHERE code='300001' AND date=?",
                       (f"2026-{d}",))
        # 缩量: 当日 5e7 vs 近5日均 2e8 = 25% < 70%
        db.execute("UPDATE daily_klines SET volume=5e7 WHERE code='300001' AND date='2026-08-21'")
        # 移出支撑事件
        db.execute("DELETE FROM stock_limit_up_down WHERE code='300001'")
        db.execute("DELETE FROM lhb_daily WHERE code='300001'")
        db.commit()

    def test_full_heat_holds(self, db):
        self._seed_all_support(db)
        a = assess_heat(db, "300001", "2026-08-21")
        assert a.recommendation == REC_HOLD and a.support_count >= 1

    def test_triple_decay_exits(self, db):
        self._seed_all_support(db)
        self._flip_to_decay(db)
        a = assess_heat(db, "300001", "2026-08-21")
        assert a.decay_count == 3
        assert a.recommendation == REC_EXIT_REVIEW

    def test_single_decay_lean_hold(self, db):
        """只有缩量一路衰减 (行业/个股流入正常, 无支撑事件) → lean_hold_review。"""
        self._seed_all_support(db)
        # 只缩量 + 移出支撑事件; 资金流保持健康
        db.execute("UPDATE daily_klines SET volume=5e7 WHERE code='300001' AND date='2026-08-21'")
        db.execute("DELETE FROM stock_limit_up_down WHERE code='300001'")
        db.execute("DELETE FROM lhb_daily WHERE code='300001'")
        db.commit()
        a = assess_heat(db, "300001", "2026-08-21")
        assert a.decay_count == 1 and a.support_count == 0
        assert a.recommendation == REC_LEAN_HOLD_REVIEW

    def test_insufficient_when_nothing_ok(self, db):
        a = assess_heat(db, "404404", "2026-08-21")  # 全空库
        assert a.decay_count == 0 and a.support_count == 0
        assert a.recommendation == REC_INSUFFICIENT

    def test_signal_exception_does_not_kill_assessment(self, db):
        """单路炸 → 该路降级, 整体仍出结论。"""
        self._seed_all_support(db)
        import strategy_layer.lifecycle.heat as heat_mod

        orig = heat_mod.sector_outflow

        def boom(*a, **k):
            raise ConnectionError("sector feed down")

        heat_mod.sector_outflow = boom
        try:
            heat_mod.assess_heat(db, "300001", "2026-08-21")
        finally:
            heat_mod.sector_outflow = orig
        # 不抛异常即通过; 结论仍生成

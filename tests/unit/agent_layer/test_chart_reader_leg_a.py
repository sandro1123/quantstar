# -*- coding: utf-8 -*-
"""#101 切1 Leg A — chart_reader 规则腿 RED (TDD)

设计稿 §3 Leg A (md5 3e477fab71ad07c7caed760be26da7c5):
- 支撑/压力: 日线 pivot 分形 (N=5 日窗口局部高低点) + 0.5×ATR 内聚类合并
  + 触碰次数定强弱 → 近端支撑 S1/S2、近端压力 R1/R2
- 量能: 量比 (当日/20日均量)、回踩段累计量 vs 突破段累计量 (缩量回踩=健康)
- 产出 technical_levels JSON: {S1,S2,R1,R2,vol_ratio,pullback_vol_ratio,note}

层间契约 (§1.5): 09 答题工人只写自家结论表 chart_analysis, 表即跨层界面;
K线/候选集 SQL 直读, 零函数级跨层调用。

切1 落库行语义 (source 枚举严格设计稿 §4):
- atr_floor 行 = LegA levels + 纯 ATR 公式带 (与 02 SignalGenerator 同式:
  entry=price±0.5×ATR / stop=2×ATR / profit=3×ATR) — 无 LLM 依赖, 切1 即产
- llm_chart 行 = 切2 LLM 读图结论 (本文件不测, 切2 RED 覆盖)
- 双录对照 = 同 (code,date) 两行并存, PK (code,date,source)
"""
import json

import pytest

from common.database import Database
from agent_layer.chart_reader.levels import (PivotDetector, LevelClusterer, VolumeAnalyzer, build_levels)


# ---------------------------------------------------------------- helpers
def _mk_db():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL, pre_close REAL, change_pct REAL,
            turnover_rate REAL, adjust_type TEXT
        )""")
    conn.commit()
    return db


def _insert_klines(conn, code, rows):
    """rows: list[(date, high, low, close, volume)] — 生产 daily_klines 真列面"""
    conn.executemany(
        "INSERT INTO daily_klines (code, date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(code, d, c, h, l, c, v) for (d, h, l, c, v) in rows],
    )
    conn.commit()


# ================================================================ 组1: pivot 分形
class TestPivotFractal:
    """N=5 日窗口局部极值: 高点=严格高于前后各5根, 低点同理。"""

    def _klines(self, highs, lows):
        # 21~22 根, 日期真实递增 (2026-01-01 起)
        return [(f"2026-01-{i + 1:02}", h, l, (h + l) / 2, 1000)
                for i, (h, l) in enumerate(zip(highs, lows))]

    def test_pivot_high_detected(self):
        """单峰形态: 第11日为前后各5根内最高 → pivot high"""
        highs = [10.0] * 5 + [10.5] * 5 + [12.0] + [10.5] * 5 + [10.0] * 5
        lows = [x - 1 for x in highs]
        det = PivotDetector(n=5)
        pivots = det.find_pivots(self._klines(highs, lows))
        phs = [p for p in pivots if p.kind == "high"]
        assert any(abs(p.price - 12.0) < 1e-9 for p in phs)

    def test_pivot_low_detected(self):
        """单谷形态: 第11日为前后各5根内最低 → pivot low"""
        lows = [10.0] * 5 + [9.5] * 5 + [8.0] + [9.5] * 5 + [10.0] * 5
        highs = [x + 1 for x in lows]
        det = PivotDetector(n=5)
        pivots = det.find_pivots(self._klines(highs, lows))
        pls = [p for p in pivots if p.kind == "low"]
        assert any(abs(p.price - 8.0) < 1e-9 for p in pls)

    def test_flat_market_no_pivot(self):
        """全程平坦: 无局部极值 → 零 pivot (宁缺毋滥, 严格>语义)"""
        flat = [(10.0, 9.5)] * 21
        det = PivotDetector(n=5)
        pivots = det.find_pivots(self._klines(*zip(*flat)))
        assert pivots == []

    def test_n_window_exclusive(self):
        """N=5 边界: 两峰相距 6 日, 各自前5后5不互含 → 两个 pivot high"""
        highs = [10.0] * 10 + [11.5] + [10.0] * 5 + [11.8] + [10.0] * 5
        lows = [x - 1 for x in highs]
        det = PivotDetector(n=5)
        pivots = det.find_pivots(self._klines(highs, lows))
        phs = [round(p.price, 2) for p in pivots if p.kind == "high"]
        assert 11.5 in phs and 11.8 in phs


# ================================================================ 组2: 聚类合并
class TestLevelClustering:
    """0.5×ATR 内同侧 pivot 合并; 触碰次数 (合并数) 定强弱; S1/S2/R1/R2 排序。"""

    def test_cluster_merge_within_half_atr(self):
        """ATR=1.0: 10.0/10.3/10.4 三个低点 pivot 互距<0.5×ATR → 合并一簇,
        touch_count=3 (最强支撑), 价格=簇内均值"""
        cl = LevelClusterer(atr=1.0)
        lvl = cl.cluster([
            ("low", 10.0, "2026-06-01"), ("low", 10.3, "2026-06-15"),
            ("low", 10.4, "2026-07-20"),
        ])
        s = [x for x in lvl if x.kind == "support"]
        assert len(s) == 1
        assert s[0].touch_count == 3
        assert abs(s[0].price - (10.0 + 10.3 + 10.4) / 3) < 1e-9

    def test_no_merge_across_half_atr(self):
        """ATR=1.0: 10.0 与 11.0 相距 1.0 > 0.5×ATR → 两个独立 level"""
        cl = LevelClusterer(atr=1.0)
        lvl = cl.cluster([
            ("low", 10.0, "d1"), ("low", 11.0, "d2"),
        ])
        s = [x for x in lvl if x.kind == "support"]
        assert len(s) == 2

    def test_support_resistance_split_and_order(self):
        """以当前价 10.5 分割: 低于现价=支撑 (S1近S2远), 高于=压力 (R1近R2远)"""
        cl = LevelClusterer(atr=1.0, current_price=10.5)
        lvl = cl.cluster([
            ("low", 10.2, "d1"), ("low", 9.5, "d2"),
            ("high", 10.9, "d3"), ("high", 11.6, "d4"),
        ])
        s = [x for x in lvl if x.kind == "support"]
        r = [x for x in lvl if x.kind == "resistance"]
        assert [x.price for x in s] == [10.2, 9.5]  # 近端在前
        assert [x.price for x in r] == [10.9, 11.6]

    def test_touch_count_stronger_first(self):
        """触碰次数多的 level 排前面; 同 touch 按距现价近的在前"""
        cl = LevelClusterer(atr=1.0, current_price=10.5)
        lvl = cl.cluster([
            ("low", 10.2, "d1"), ("low", 10.21, "d2"), ("low", 10.19, "d3"),
            ("low", 9.0, "d4"),
        ])
        s = [x for x in lvl if x.kind == "support"]
        assert s[0].touch_count == 3 and s[0].price == pytest.approx(10.2)
        assert s[1].touch_count == 1


# ================================================================ 组3: 量比
class TestVolumeAnalyzer:
    """量比=当日量/前20日均量; 回踩段 vs 突破段累计量 (缩量回踩=健康)。"""

    def _row(self, d, v):
        return (d, 10.5, 10.0, 10.2, v)

    def test_vol_ratio_same_day_over_20d_avg(self):
        """当日量 3000, 前20日均量 1000 → vol_ratio=3.0"""
        db = _mk_db()
        conn = db.get_connection()
        _insert_klines(conn, "000001",
                        [self._row(f"2026-08-{i + 1:02}", 1000) for i in range(20)] +
                        [self._row("2026-08-21", 3000)])
        va = VolumeAnalyzer()
        r = va.compute("000001", "2026-08-21", conn)
        assert r["vol_ratio"] == pytest.approx(3.0)
        db.close()

    def test_pullback_vs_breakout_volume(self):
        """突破段 (最近 pivot high 收尾的前5根放量上攻) vs 回踩段 (pivot 后至今):
        回踩/突破 = 10200/35000 ≈ 0.29 < 1 → 缩量回踩 (健康)"""
        db = _mk_db()
        conn = db.get_connection()
        rows = []
        # 7/26..7/31 平台期: 铺足 pivot 前窗 (n=5 分形需 8/5 前有 ≥5 根)
        for i in range(26, 32):
            rows.append((f"2026-07-{i}", 9.5, 9.0, 9.2, 800))
        # 8/1..8/5 突破段放量: 5000 6000 7000 8000 9000 — 8/5 峰 12.0 =
        # pivot high (前后各5根高点均 < 12.0)
        for i, v in enumerate([5000, 6000, 7000, 8000, 9000]):
            rows.append((f"2026-08-{i + 1:02}", 12.0 if i == 4 else 10.5,
                         9.8, 10.2, v))
        # 8/6..8/10 回踩段缩量: 3000 2500 2000 1500 1200
        for i, v in enumerate([3000, 2500, 2000, 1500, 1200]):
            rows.append((f"2026-08-{i + 6:02}", 10.8, 9.9, 10.3, v))
        _insert_klines(conn, "000001", rows)
        va = VolumeAnalyzer()
        r = va.compute("000001", "2026-08-10", conn)
        assert r["pullback_vol_ratio"] == pytest.approx(10200 / 35000, rel=1e-3)
        db.close()

    def test_insufficient_history_returns_none_fields(self):
        """历史不足 20 日: vol_ratio/pullback_vol_ratio = None (宁缺毋滥)"""
        db = _mk_db()
        conn = db.get_connection()
        _insert_klines(conn, "000001",
                        [self._row(f"2026-08-{i + 1:02}", 1000) for i in range(5)])
        va = VolumeAnalyzer()
        r = va.compute("000001", "2026-08-05", conn)
        assert r["vol_ratio"] is None
        assert r["pullback_vol_ratio"] is None
        db.close()


# ================================================================ 组4: 表迁移 v6 + 落库
class TestChartAnalysisMigrationAndPersist:
    """v6: chart_analysis 表进中央迁移器 (不走管道内手写 DDL, 设计稿铁律)。"""

    def test_migration_v6_declares_chart_analysis(self):
        from execution_layer.migrations import MIGRATIONS, TARGET_VERSION
        assert TARGET_VERSION >= 6
        by_version = dict(MIGRATIONS)
        assert any("chart_analysis" in s for s in by_version[6])

    def test_migration_v6_creates_table_idempotent(self):
        from execution_layer.migrations import TARGET_VERSION, ensure_schema
        db = Database(":memory:")
        db.init_db()
        r1 = ensure_schema(db)
        assert r1["version_after"] == TARGET_VERSION
        conn = db.get_connection()
        cols = {c[1] for c in conn.execute("PRAGMA table_info(chart_analysis)")}
        # 设计稿 §4 字段全量
        assert {"code", "date", "pattern", "entry_low", "entry_high", "stop",
                "profit", "confidence", "reasons", "source", "levels_json",
                "atr_baseline", "llm_raw", "created_at"} <= cols
        # 幂等
        r2 = ensure_schema(db)
        assert r2["applied"] == []
        db.close()

    def test_baseline_row_persist_roundtrip(self):
        """切1 落库面: atr_floor 行 = LegA levels_json + ATR 公式带,
        PK (code,date,source) 幂等重写"""
        from agent_layer.chart_reader.repository import ChartAnalysisRepository
        from execution_layer.migrations import ensure_schema
        db = Database(":memory:")
        db.init_db()
        ensure_schema(db)
        repo = ChartAnalysisRepository(db)
        levels = {"S1": 10.2, "S2": 9.5, "R1": 10.9, "R2": 11.6,
                  "vol_ratio": 3.0, "pullback_vol_ratio": 0.29, "note": "缩量回踩"}
        # price=10.5, atr=1.0 → 02 同式公式带: entry 10.0~11.0 / stop 8.5 / profit 13.5
        repo.write_baseline("000001", "2026-08-10", levels,
                            current_price=10.5, atr=1.0)
        row = repo.get("000001", "2026-08-10", "atr_floor")
        assert row is not None
        assert json.loads(row["levels_json"]) == levels
        assert row["source"] == "atr_floor"
        assert row["atr_baseline"] == 1.0
        # ATR 公式带 = 02 SignalGenerator 同式 (bit-exact 对照源)
        assert row["entry_low"] == 10.0
        assert row["entry_high"] == 11.0
        assert row["stop"] == 8.5
        assert row["profit"] == 13.5
        assert row["llm_raw"] is None
        # 幂等: 同日重写不产双行
        repo.write_baseline("000001", "2026-08-10", levels,
                            current_price=10.5, atr=1.0)
        assert repo.count_for_date("2026-08-10") == 1
        db.close()


# ================================================================ 组5: LegA 全链
class TestBuildLevelsChain:
    """klines → pivot 分形 → 聚类 → S/R 近端映射 + 量能 → technical_levels (§3 产出)。"""

    def test_build_levels_full_chain(self):
        """31根K线: 谷8.0(pivot low) + 峰12.0(pivot high) + 缩量回踩 →
        S1=8.0 / R1=12.0 / S2=R2=None / note=缩量回踩"""
        db = _mk_db()
        conn = db.get_connection()
        rows = []
        for i in range(1, 6):        # 8/1-8/5 蓄势
            rows.append((f"2026-08-{i:02}", 10.0, 9.5, 9.8, 1000))
        for i in range(6, 11):      # 8/6-8/10 下行
            rows.append((f"2026-08-{i:02}", 9.7, 9.2, 9.5, 1000))
        rows.append(("2026-08-11", 8.8, 8.0, 8.4, 1000))     # 谷 → pivot low 8.0
        for i in range(12, 17):     # 8/12-8/16 修复
            rows.append((f"2026-08-{i:02}", 9.6, 9.0, 9.3, 1000))
        for i in range(17, 21):     # 8/17-8/20 攻势
            rows.append((f"2026-08-{i:02}", 10.0, 9.4, 9.7, 1000))
        rows.append(("2026-08-21", 12.0, 9.8, 11.5, 5000))    # 峰 → pivot high 12.0
        for i in range(22, 27):     # 8/22-8/26 缩量回踩
            rows.append((f"2026-08-{i:02}", 10.8, 10.0, 10.4, 600))
        for i in range(27, 32):     # 8/27-8/31 企稳 (当日=8/31, 收盘10.5)
            rows.append((f"2026-08-{i:02}", 10.9, 10.1, 10.5, 400))
        _insert_klines(conn, "000001", rows)
        lv = build_levels("000001", "2026-08-31", conn, atr=1.0)
        assert lv is not None
        assert lv["S1"] == pytest.approx(8.0)
        assert lv["R1"] == pytest.approx(12.0)
        assert lv["S2"] is None
        assert lv["R2"] is None
        # vol_ratio = 当日400 / 前20日均量 (19600/20=980)
        assert lv["vol_ratio"] == pytest.approx(400 / 980)
        # 突破段=8/17-8/21 (1000*4+5000=9000); 回踩段=8/22-8/31 (600*5+400*5=5000)
        assert lv["pullback_vol_ratio"] == pytest.approx(5000 / 9000)
        assert lv["note"] == "缩量回踩"
        db.close()

    def test_build_levels_missing_day_returns_none(self):
        """当日无K线 → None (宁缺毋滥, 不硬造)"""
        db = _mk_db()
        conn = db.get_connection()
        _insert_klines(conn, "000001",
                       [(f"2026-08-{i + 1:02}", 10.0, 9.5, 9.8, 1000) for i in range(10)])
        assert build_levels("000001", "2026-08-15", conn, atr=1.0) is None
        db.close()

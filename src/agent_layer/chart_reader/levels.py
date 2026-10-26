# -*- coding: utf-8 -*-
"""chart_reader Leg A — 规则腿: pivot 分形 / level 聚类 / 量能结构 (#101 切1)

设计稿 §3 Leg A (全确定性, 零 LLM):
- 支撑/压力: 日线 pivot 分形 (N=5 日窗口局部高低点) + 0.5×ATR 内聚类合并
  + 触碰次数定强弱 → 近端支撑 S1/S2、近端压力 R1/R2
- 量能结构: 量比 (当日量/前20日均量)、回踩段累计量 vs 突破段累计量
  (突破段 = 最近 pivot high 收尾的前5根; 回踩段 = pivot 后至今; 缩量回踩=健康)
- 产出 technical_levels: {S1,S2,R1,R2,vol_ratio,pullback_vol_ratio,note}

层间契约 (§1.5): 本模块纯计算 + SQL 直读 daily_klines, 零函数级跨层调用。
宁缺毋滥: 窗口不足/无分形/除零 → None, 不硬造数值。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Pivot:
    """分形极值点。kind: "high" (压力源) / "low" (支撑源)。"""

    kind: str
    price: float
    date: str | None = None
    index: int | None = None


@dataclass(frozen=True)
class Level:
    """聚类后的价位带。kind: "support" / "resistance"; touch_count = 合并 pivot 数。"""

    kind: str
    price: float
    touch_count: int


class PivotDetector:
    """N 日窗口分形: 高点 = 严格高于前后各 N 根 (平坦即无点, 宁缺毋滥)。"""

    def __init__(self, n: int = 5):
        if n < 1:
            raise ValueError("n must be >= 1")
        self.n = n

    def find_pivots(self, rows: Sequence) -> list[Pivot]:
        """rows: (date, high, low, close, volume) 位置序列 (tuple / sqlite3.Row 皆可)。

        要求完整窗口 (前后各 N 根都在) — 边缘一根不齐即不算分形。
        """
        n = self.n
        total = len(rows)
        pivots: list[Pivot] = []
        for i in range(n, total - n):
            window = range(i - n, i + n + 1)
            highs = [rows[j][1] for j in window if j != i]
            lows = [rows[j][2] for j in window if j != i]
            high, low, date = rows[i][1], rows[i][2], rows[i][0]
            if high > max(highs):
                pivots.append(Pivot("high", float(high), date, i))
            if low < min(lows):
                pivots.append(Pivot("low", float(low), date, i))
        return pivots


class LevelClusterer:
    """0.5×ATR 内价格带聚类合并; 触碰次数 (合并 pivot 数) 定强弱。

    现价分割: 低于现价 = 支撑, 高于 = 压力 (无现价则按 pivot 属性多数)。
    排序: touch 多者强在前; 同 touch 距现价近者在前 (无现价 → 支撑价高/压力价低在前)。
    """

    def __init__(self, atr: float, current_price: float | None = None):
        if atr <= 0:
            raise ValueError("atr must be positive")
        self.atr = float(atr)
        self.current_price = current_price

    def cluster(self, items: Iterable[tuple]) -> list[Level]:
        """items: (kind, price, date) 序列, kind ∈ "high"/"low"。"""
        half = self.atr * 0.5
        pts = sorted((float(p), str(k), d) for k, p, d in items)
        # 贪心价格带聚类: 与当前簇均值互距 ≤ 0.5×ATR 即并入
        groups: list[list[float]] = []
        kinds: list[list[str]] = []
        for price, kind, _date in pts:
            if groups and abs(price - sum(groups[-1]) / len(groups[-1])) <= half:
                groups[-1].append(price)
                kinds[-1].append(kind)
            else:
                groups.append([price])
                kinds.append([kind])

        levels: list[Level] = []
        for prices, ks in zip(groups, kinds):
            price = sum(prices) / len(prices)
            if self.current_price is not None:
                lv_kind = "support" if price < self.current_price else "resistance"
            else:
                lv_kind = "support" if ks.count("low") >= ks.count("high") else "resistance"
            levels.append(Level(lv_kind, price, len(prices)))

        cp = self.current_price
        if cp is not None:
            levels.sort(key=lambda lv: (-lv.touch_count, abs(lv.price - cp)))
        else:
            levels.sort(key=lambda lv: (
                -lv.touch_count, -lv.price if lv.kind == "support" else lv.price
            ))
        return levels


class VolumeAnalyzer:
    """量能结构: 量比 (当日/前20日均量) + 回踩/突破累计量比 (缩量回踩=健康)。"""

    BREAKOUT_WINDOW = 5   # 突破段 = 最近 pivot high 收尾的前5根
    VOL_AVG_WINDOW = 20   # 量比基准 = 前20日均量

    def compute(self, code: str, date: str, conn) -> dict:
        """直读 daily_klines (SQL 即跨层界面, §1.5)。历史不足 → 对应字段 None。"""
        rows = conn.execute(
            "SELECT date, high, low, close, volume FROM daily_klines "
            "WHERE code=? AND date<=? ORDER BY date",
            (code, date),
        ).fetchall()
        return self.compute_from_rows(rows, date)

    def compute_from_rows(self, rows: Sequence, date: str) -> dict:
        result = {"vol_ratio": None, "pullback_vol_ratio": None}
        if not rows or rows[-1][0] != date:
            return result   # 当日无K线 → 宁缺毋滥
        idx = len(rows) - 1

        # 量比: 当日量 / 前20日均量
        prior = rows[:idx]
        if len(prior) >= self.VOL_AVG_WINDOW:
            window = prior[-self.VOL_AVG_WINDOW:]
            avg = sum(r[4] for r in window) / len(window)
            if avg > 0:
                result["vol_ratio"] = rows[idx][4] / avg

        # 回踩/突破量比: 最近 pivot high 为界 (扫描含当日, 当日即峰 → 回踩段空 → None)
        pivots = PivotDetector(n=5).find_pivots(rows)
        high_idx = [p.index for p in pivots if p.kind == "high" and p.index is not None]
        if high_idx:
            pivot_i = max(high_idx)   # 最近一个 pivot high
            breakout = rows[max(0, pivot_i - self.BREAKOUT_WINDOW + 1): pivot_i + 1]
            pullback = rows[pivot_i + 1: idx + 1]
            breakout_sum = sum(r[4] for r in breakout)
            if pullback and breakout_sum > 0:
                result["pullback_vol_ratio"] = sum(r[4] for r in pullback) / breakout_sum
        return result


def build_levels(code: str, date: str, conn, atr: float, n: int = 5) -> dict | None:
    """Leg A 全链产出 (§3): klines → pivot 分形 → 聚类 → S/R 近端映射 + 量能。

    Returns technical_levels: {S1,S2,R1,R2,vol_ratio,pullback_vol_ratio,note};
    S1/R1 = 近端 (触碰多者强在前, 同触距现价近者在前)。当日无K线 → None;
    无 pivot → S/R 全 None (量能仍给, 宁缺毋滥逐字段降级)。
    """
    rows = conn.execute(
        "SELECT date, high, low, close, volume FROM daily_klines "
        "WHERE code=? AND date<=? ORDER BY date",
        (code, date),
    ).fetchall()
    if not rows or rows[-1][0] != date:
        return None
    current_price = rows[-1][3]

    pivots = PivotDetector(n=n).find_pivots(rows)
    levels = LevelClusterer(atr=atr, current_price=current_price).cluster(
        (p.kind, p.price, p.date) for p in pivots
    )
    supports = [lv.price for lv in levels if lv.kind == "support"]
    resistances = [lv.price for lv in levels if lv.kind == "resistance"]

    vol = VolumeAnalyzer().compute_from_rows(rows, date)
    pvr = vol["pullback_vol_ratio"]
    note = None
    if pvr is not None:
        note = "缩量回踩" if pvr < 1 else "放量滞涨"
    return {
        "S1": supports[0] if supports else None,
        "S2": supports[1] if len(supports) > 1 else None,
        "R1": resistances[0] if resistances else None,
        "R2": resistances[1] if len(resistances) > 1 else None,
        "vol_ratio": vol["vol_ratio"],
        "pullback_vol_ratio": pvr,
        "note": note,
    }

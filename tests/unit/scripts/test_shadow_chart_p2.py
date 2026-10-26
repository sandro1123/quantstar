# -*- coding: utf-8 -*-
"""#101 切3 p2_factors 展示位 RED (TDD) — 影子模式展示面 (设计稿 §6)

§6 切3 三件之一: p2_factors 展示位 — chart_analysis 影子模式挂在
P2 因子健康页尾 (LLM 读图属 09 图形面, 不开新页)。

展示语义 (影子期, §5):
- 标题明示「影子模式」+ 6 周裁决窗口径 (入场区触达率/止损先触率/
  止盈先触率) — 裁决前 LLM 结论不入账, 展示即影子展示;
- 数据面 = chart_analysis 累计分组计数 + compare_sources 三率对照;
- 源表未迁移 (生产 v6 未跑前) 或无数据 → 自护降级为占位文案
  (marimo export 不炸 — 全页全周期 export 铁律的前置防线)。

marimo 坑 (memory: marimo 格私有下划线陷阱):
- cell 间靠返回值传递, cell 内下划线名 = 格私有不跨格;
- C.fetch_df 无 OperationalError 防护 → 新 cell 须自护
  (import sqlite3 + try/except 占位降级)。

本文件钉两层: shadow_chart 模块 (页同目录, 纯函数层) + 页接线
(p2_factors 源文本含 import/调用/自护)。测试先例:
test_build_marimo_period.py (sys.path 挂 scripts) + 源文本断言先例
test_dispatch_wires_chart_analysis (inspect.getsource)。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PAGES = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "marimo_pages"
sys.path.insert(0, str(_PAGES))

import shadow_chart  # noqa: E402  (RED: 页同目录展示函数模块, 先红)

D = "2026-08-20"


# ---------------------------------------------------------------- helpers
def _mk(tmp_path):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chart_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL, date TEXT NOT NULL, pattern TEXT,
            entry_low REAL, entry_high REAL, stop REAL, profit REAL,
            confidence REAL, reasons TEXT, source TEXT NOT NULL,
            levels_json TEXT, atr_baseline REAL, llm_raw TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
            UNIQUE (code, date, source)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL, pre_close REAL, change_pct REAL,
            turnover_rate REAL, adjust_type TEXT
        )""")
    return conn


def _chart(conn, code, source, pattern="缩量回踩突破", date=D,
           entry_low=10.0, entry_high=11.0, stop=8.5, profit=13.5):
    """播种全值行 (裸 4 列 = NULL 值行, 测不了三率 — 夹具笔误修正)。"""
    conn.execute(
        "INSERT INTO chart_analysis (code, date, pattern, entry_low, "
        "entry_high, stop, profit, source) VALUES (?,?,?,?,?,?,?,?)",
        (code, date, pattern, entry_low, entry_high, stop, profit, source))
    conn.commit()


def _kline(conn, code, date, high, low):
    conn.execute(
        "INSERT INTO daily_klines (code, date, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?)", (code, date, low + 0.1, high, low, low + 0.2, 100))
    conn.commit()


# ================================================================ 组1: 三率统计
class TestShadowStats:
    def test_shadow_stats_counts(self, tmp_path):
        """三率统计: 分组计数 + 触达/止损先触/止盈先触 + degraded 单列"""
        conn = _mk(tmp_path)
        _chart(conn, "000001", "atr_floor")
        _chart(conn, "000001", "llm_chart")
        _chart(conn, "000002", "llm_chart", pattern=None)   # degraded
        _kline(conn, "000001", "2026-08-21", 14.0, 10.5)    # 双组止盈先触
        _kline(conn, "000002", "2026-08-21", 14.0, 10.5)
        st = shadow_chart.shadow_stats(conn)
        assert st["atr_floor"]["rows"] == 1
        assert st["atr_floor"]["evaluated"] == 1
        assert st["atr_floor"]["entry_touch"] == 1
        assert st["atr_floor"]["profit_first"] == 1
        assert st["atr_floor"]["stop_first"] == 0
        assert st["llm_chart"]["rows"] == 1    # degraded 不入 llm 组 (切2 判别键)
        assert st["degraded_rows"] == 1
        conn.close()

    def test_shadow_stats_no_forward_rates_none(self, tmp_path):
        """无前视窗 → evaluated=0, 三率 None (不造0%, 与 evaluation 模块同口径)"""
        conn = _mk(tmp_path)
        _chart(conn, "000001", "atr_floor")
        _chart(conn, "000001", "llm_chart")
        st = shadow_chart.shadow_stats(conn)
        for grp in ("atr_floor", "llm_chart"):
            assert st[grp]["evaluated"] == 0
            assert st[grp]["entry_touch_rate"] is None
            assert st[grp]["stop_first_rate"] is None
            assert st[grp]["profit_first_rate"] is None
        conn.close()

    def test_shadow_stats_missing_table_graceful(self, tmp_path):
        """无 chart_analysis 表 (生产未迁移态) → 全零结构不炸 (export 前置防线)"""
        conn = _mk(tmp_path)
        conn.execute("DROP TABLE chart_analysis")
        conn.commit()
        st = shadow_chart.shadow_stats(conn)
        assert st["atr_floor"]["rows"] == 0
        assert st["llm_chart"]["rows"] == 0
        assert st["degraded_rows"] == 0
        conn.close()

    def test_shadow_stats_missing_klines_graceful(self, tmp_path):
        """chart_analysis 有行但 daily_klines 无表 → 行计数在, 评估静默不炸"""
        conn = _mk(tmp_path)
        conn.execute("DROP TABLE daily_klines")
        conn.commit()
        _chart(conn, "000001", "atr_floor")
        st = shadow_chart.shadow_stats(conn)
        assert st["atr_floor"]["rows"] == 1
        assert st["atr_floor"]["evaluated"] == 0
        assert st["atr_floor"]["entry_touch_rate"] is None
        conn.close()


# ================================================================ 组2: 展示 HTML
class TestShadowDisplay:
    def test_display_html_contains_shadow_semantics(self, tmp_path):
        """HTML 面含影子语义词 + 双组标签 + 三率名 (6周裁决窗口径)"""
        conn = _mk(tmp_path)
        _chart(conn, "000001", "atr_floor")
        _chart(conn, "000001", "llm_chart")
        _kline(conn, "000001", "2026-08-21", 11.8, 10.5)   # 触入场带
        _kline(conn, "000001", "2026-08-22", 14.0, 11.0)   # 止盈先触
        html = shadow_chart.shadow_display_html(conn)
        assert "影子模式" in html
        assert "入场区触达率" in html
        assert "止损先触率" in html
        assert "止盈先触率" in html
        assert "atr_floor" in html and "LLM" in html   # 双组标签
        conn.close()

    def test_display_html_placeholder_when_missing(self, tmp_path):
        """无表 → 占位文案 (含未迁移语义), export 不炸"""
        conn = _mk(tmp_path)
        conn.execute("DROP TABLE chart_analysis")
        conn.commit()
        html = shadow_chart.shadow_display_html(conn)
        assert "影子模式" in html            # 段标题仍在 (段位恒定)
        assert "未迁移" in html or "无数据" in html
        conn.close()


# ================================================================ 组3: 页接线 (源文本)
class TestPageWiring:
    def test_p2_page_wires_shadow_section(self):
        """p2_factors 尾部新 cell: import shadow_chart + 调用 + OperationalError 自护"""
        src = (_PAGES / "p2_factors.py").read_text(encoding="utf-8")
        assert "import shadow_chart" in src          # 首格已挂 marimo_pages 路径
        assert "shadow_chart.shadow_display_html" in src
        assert "OperationalError" in src             # 新 cell 自护 (C.fetch_df 无防护)

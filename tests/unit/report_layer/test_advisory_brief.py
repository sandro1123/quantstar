"""卡VII 三时点投顾 — advisory_brief 函数层 + 三 section 红绿测试

设计依据 (统筹裁定 sm-c525b45463d94435 三分叉全裁):
1. report_layer 新 section 搭晨/午既有管线 + 公共函数层
   advisory_brief.py (section 与 CLI 共用, 防口径分叉)。
2. 同链 report_layer build+push (qscard 不动 — 留给盘中触价卡)。
3. 尾盘时点 = 14:52 (非整点), 晨=开盘前隔夜+昨收 / 午=上午实时 / 尾=午后实时。

只读铁律 (统筹同裁定): 投顾简报任何一段不得写库。
   断言方式 = total_changes 探针 (qs-fcdb closed-database P0 同款判别法),
   函数层三时点各一测, section 层 build 全程 (含渲染) 再统一探针。

hermetic: 内存 sqlite, 与 advisory CLI 同 schema; 日期用滚动常量抗漂移
(Q7/canrun 病根教训: 不硬编码施工当日)。
"""
import sqlite3
from datetime import date as _date
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from report_layer.reports.base import ReportContext
from report_layer.sections.advisory_brief import (
    TIME_POINT_LABELS,
    AdvisoryMiddaySection,
    AdvisoryMorningSection,
    AdvisoryTailSection,
    build_advisory_brief,
)

# ── 抗漂移日期 (滚动, 不硬编码施工当日) ──────────────────
PRED_DATE = str(_date.today())                     # 评分日=今日
OLD_ENTRY_DATE = str(_date.today() - timedelta(days=3))  # 持仓 3 天


SCHEMA = """
CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT,
    market TEXT, list_date TEXT, is_active INTEGER, delist_date TEXT);
CREATE TABLE model_predictions (date TEXT, code TEXT, ml_score REAL,
    factor_score REAL, blended_score REAL);
CREATE TABLE stock_pool (code TEXT, name TEXT, status TEXT, entry_price REAL,
    last_price REAL, position_qty INTEGER, unrealized_pnl_pct REAL,
    holding_days INTEGER, stop_loss REAL, dynamic_stop_loss REAL,
    take_profit REAL, entry_date TEXT, exit_reason TEXT);
CREATE TABLE daily_regime (date TEXT, regime TEXT);
CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL);
"""


def _mkdb() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 候选: 两只 (688349 分高在池 / 600519 分低不在池)
    conn.execute("INSERT INTO stock_info VALUES ('688349','昊海生科','医疗','SH','2019-04-30',1,NULL)")
    conn.execute("INSERT INTO stock_info VALUES ('600519','贵州茅台','白酒','SH','2001-08-27',1,NULL)")
    conn.execute("INSERT INTO model_predictions VALUES (?,?,80.0,80.0,82.5)",
                 (PRED_DATE, "688349"))
    conn.execute("INSERT INTO model_predictions VALUES (?,?,70.0,70.0,79.1)",
                 (PRED_DATE, "600519"))
    # 旧日期高分数据 — 不得入选 (口径: 只取最新日期)
    conn.execute("INSERT INTO model_predictions VALUES ('2026-01-01','000001',99,99,99.0)")
    # 持仓: 3 天前入场, 有现价有盈亏 (非今日入场, 走正常盈亏分支)
    conn.execute("INSERT INTO stock_pool VALUES ('688349','昊海生科','active',29.0,"
                 "30.5,800,5.17,3,28.0,28.5,32.0,?,NULL)", (OLD_ENTRY_DATE,))
    conn.execute("INSERT INTO daily_regime VALUES (?,'neutral')", (PRED_DATE,))
    conn.commit()
    return conn


def _ctx(conn, date_str=None):
    data = MagicMock()
    db = MagicMock()
    db.get_connection.return_value = conn
    data.db = db
    data.get_realtime_prices.return_value = {}
    return ReportContext(date=date_str or PRED_DATE, data_layer=data)


class _Probe:
    """只读探针: sqlite total_changes 任何写操作后递增。"""

    def __init__(self, conn):
        self.conn = conn
        self._before = conn.total_changes

    def assert_zero_writes(self):
        assert self.conn.total_changes == self._before, \
            "只读铁律违约: 投顾简报发生了写库操作!"


@pytest.fixture()
def ro_db():
    return _mkdb()


# ── 函数层: build_advisory_brief ─────────────────────────

class TestBuildAdvisoryBrief:
    def test_returns_dict_with_time_point(self, ro_db):
        result = build_advisory_brief(ro_db, "morning")
        assert isinstance(result, dict)
        assert result["time_point"] == "morning"

    def test_three_time_points_all_supported(self, ro_db):
        for tp in ("morning", "midday", "tail"):
            result = build_advisory_brief(ro_db, tp)
            assert result["time_point"] == tp

    def test_invalid_time_point_rejected(self, ro_db):
        with pytest.raises(ValueError):
            build_advisory_brief(ro_db, "invalid_tp")

    def test_readonly_iron_law_morning(self, ro_db):
        probe = _Probe(ro_db)
        build_advisory_brief(ro_db, "morning")
        probe.assert_zero_writes()

    def test_readonly_iron_law_midday(self, ro_db):
        probe = _Probe(ro_db)
        build_advisory_brief(ro_db, "midday")
        probe.assert_zero_writes()

    def test_readonly_iron_law_tail(self, ro_db):
        probe = _Probe(ro_db)
        build_advisory_brief(ro_db, "tail")
        probe.assert_zero_writes()

    def test_candidates_latest_date_only(self, ro_db):
        """口径对齐 CLI candidates: 只取 model_predictions 最新日期 topN。"""
        result = build_advisory_brief(ro_db, "morning")
        codes = [c["code"] for c in result["candidates"]]
        assert "688349" in codes and "600519" in codes
        assert "000001" not in codes  # 旧日期高分不得混入

    def test_candidates_ranked_desc_with_pool_flag(self, ro_db):
        result = build_advisory_brief(ro_db, "morning")
        scores = [c["blended_score"] for c in result["candidates"]]
        assert scores == sorted(scores, reverse=True)
        top = result["candidates"][0]
        assert top["in_pool"] is True  # 688349 已在池

    def test_holdings_active_with_pnl(self, ro_db):
        """口径对齐 CLI holdings: active 持仓带现价盈亏, join 真名。"""
        result = build_advisory_brief(ro_db, "midday")
        holdings = result["holdings"]
        assert len(holdings) == 1
        h = holdings[0]
        assert h["code"] == "688349"
        assert h["name"] == "昊海生科"
        assert h["unrealized_pnl_pct"] == 5.17

    def test_holdings_today_entry_honest_null(self, ro_db):
        """范式7: 今日入场无新价 → 盈亏 NULL 不造假。"""
        ro_db.execute("UPDATE stock_pool SET entry_date = ? WHERE code = '688349'",
                      (PRED_DATE,))
        ro_db.commit()
        result = build_advisory_brief(ro_db, "midday", today=PRED_DATE)
        h = result["holdings"][0]
        assert h["unrealized_pnl_pct"] is None
        assert "待结算" in h["note_zh"]

    def test_regime_present(self, ro_db):
        result = build_advisory_brief(ro_db, "tail")
        assert result["regime_zh"]  # 非空 (中性)

    def test_empty_db_honest_not_fake(self):
        """诚实语义: 空库返回结构化空, 不造假不抛。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        result = build_advisory_brief(conn, "morning")
        assert result["candidates"] == []
        assert result["holdings"] == []
        assert result["regime_zh"] == ""
        conn.close()


# ── Section 层: 三时点 section ───────────────────────────

class TestAdvisorySections:
    def test_morning_section_renders(self, ro_db):
        sec = AdvisoryMorningSection()
        result = sec.build(_ctx(ro_db))
        assert result is not None
        assert result.title
        assert "688349" in result.body
        assert result.sources  # 段级 source 溯源

    def test_midday_section_renders(self, ro_db):
        sec = AdvisoryMiddaySection()
        result = sec.build(_ctx(ro_db))
        assert result is not None
        assert result.title
        assert "688349" in result.body

    def test_tail_section_renders(self, ro_db):
        sec = AdvisoryTailSection()
        result = sec.build(_ctx(ro_db))
        assert result is not None
        assert result.title
        assert "688349" in result.body

    def test_sections_all_readonly(self, ro_db):
        """只读铁律 section 层: build 全程 (含渲染) 零写库。"""
        probe = _Probe(ro_db)
        for sec in (AdvisoryMorningSection(), AdvisoryMiddaySection(),
                    AdvisoryTailSection()):
            sec.build(_ctx(ro_db))
        probe.assert_zero_writes()

    def test_section_names_distinct(self, ro_db):
        names = {AdvisoryMorningSection().name, AdvisoryMiddaySection().name,
                 AdvisoryTailSection().name}
        assert len(names) == 3

    def test_time_point_labels(self):
        assert "morning" in TIME_POINT_LABELS
        assert "midday" in TIME_POINT_LABELS
        assert "tail" in TIME_POINT_LABELS
        assert all(isinstance(v, str) for v in TIME_POINT_LABELS.values())

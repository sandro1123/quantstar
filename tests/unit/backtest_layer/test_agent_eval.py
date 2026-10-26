"""AgentEvaluator 单元测试 (BT-09)

覆盖: 命中判定语义与 Agent 层 Evaluator 对齐、百分数→小数单位转换、
未到期预测计 unevaluated 不造假、分组统计、可评估日查找。
用真实 in-memory sqlite (row_factory=Row) 而非 mock, 保证 SQL 可执行。
"""
import sqlite3

import pytest

from backtest_layer.evaluation.agent_eval import (
    AgentEvalResult,
    AgentEvaluator,
    confidence_tier,
    is_hit,
)


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE agent_predictions ("
        "id INTEGER PRIMARY KEY, agent_name TEXT, code TEXT, date TEXT, "
        "verdict TEXT, confidence REAL, actual_return REAL, hit INTEGER)")
    conn.execute(
        "CREATE TABLE factor_daily ("
        "code TEXT, date TEXT, fwd_return_5d REAL)")
    return conn


class _Db:
    """最小 db 适配: AgentEvaluator 只用 get_connection()"""

    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


class TestIsHit:
    def test_recommend_up_is_hit(self):
        assert is_hit("推荐", 0.03) is True
        assert is_hit("approve", 0.01) is True

    def test_recommend_down_is_miss(self):
        assert is_hit("推荐", -0.01) is False

    def test_reject_down_is_hit(self):
        assert is_hit("不推荐", -0.02) is True
        assert is_hit("reject", -0.001) is True

    def test_reject_up_is_miss(self):
        assert is_hit("不推荐", 0.02) is False

    def test_neutral_small_move_is_hit(self):
        # 与 Agent 层 Evaluator 一致: abs(actual) < 0.02
        assert is_hit("观望", 0.019) is True
        assert is_hit("neutral", -0.019) is True
        assert is_hit("观望", 0.02) is False  # 边界: 不小于 2% 才算命中

    def test_warning_never_hits(self):
        assert is_hit("警惕", 0.05) is False
        assert is_hit("警惕", -0.05) is False


class TestConfidenceTier:
    def test_tiers(self):
        assert confidence_tier(0.9) == "high"
        assert confidence_tier(0.6) == "high"
        assert confidence_tier(0.5) == "mid"
        assert confidence_tier(0.4) == "mid"
        assert confidence_tier(0.39) == "low"
        assert confidence_tier(None) == "unknown"


class TestForwardReturns:
    def test_percent_to_decimal(self):
        """库内 fwd_return_5d 为百分数 (1.92 = +1.92%), 产出必须转小数"""
        conn = _make_db()
        conn.execute(
            "INSERT INTO factor_daily VALUES ('000001', '2026-08-07', 1.92)")
        conn.execute(
            "INSERT INTO factor_daily VALUES ('000002', '2026-08-07', -3.5)")
        conn.execute(
            "INSERT INTO factor_daily VALUES ('000003', '2026-08-07', NULL)")
        ev = AgentEvaluator(_Db(conn))
        fwd = ev.get_forward_returns("2026-08-07")
        assert fwd["000001"] == pytest.approx(0.0192)
        assert fwd["000002"] == pytest.approx(-0.035)
        assert "000003" not in fwd  # NULL 不产出, 不冒充 0

    def test_no_db_returns_empty(self):
        assert AgentEvaluator(None).get_forward_returns("2026-08-07") == {}


class TestEvaluate:
    def _seed(self, conn):
        # 2026-08-07: 4 条预测, 3 条能配到前向收益, 1 条未到期
        preds = [
            ("quick_scan", "000001", "推荐", 0.8),     # +1.92% → 命中
            ("quick_scan", "000002", "不推荐", 0.7),   # -3.5%  → 命中
            ("normal_analysis1", "000003", "观望", 0.5),  # +4.0% → 未命中
            ("normal_analysis1", "000004", "推荐", 0.65),  # 无 fwd → unevaluated
        ]
        for agent, code, verdict, conf in preds:
            conn.execute(
                "INSERT INTO agent_predictions "
                "(agent_name, code, date, verdict, confidence) "
                "VALUES (?, ?, '2026-08-07', ?, ?)",
                (agent, code, verdict, conf))
        fwd = [("000001", 1.92), ("000002", -3.5), ("000003", 4.0)]
        for code, r in fwd:
            conn.execute(
                "INSERT INTO factor_daily VALUES (?, '2026-08-07', ?)",
                (code, r))

    def test_evaluated_vs_unevaluated(self):
        conn = _make_db()
        self._seed(conn)
        result = AgentEvaluator(_Db(conn)).evaluate("2026-08-07")
        assert isinstance(result, AgentEvalResult)
        assert result.total_predictions == 4
        assert result.evaluated == 3
        assert result.unevaluated == 1  # 000004 未到期, 不计命中/未命中
        assert result.hits == 2
        assert result.hit_rate == pytest.approx(2 / 3, abs=1e-4)

    def test_group_buckets(self):
        conn = _make_db()
        self._seed(conn)
        result = AgentEvaluator(_Db(conn)).evaluate("2026-08-07")
        assert result.by_verdict["推荐"]["evaluated"] == 1
        assert result.by_verdict["推荐"]["hits"] == 1
        assert result.by_verdict["观望"]["hit_rate"] == 0.0
        assert result.by_agent["quick_scan"]["hits"] == 2
        assert result.by_agent["normal_analysis1"]["evaluated"] == 1
        # 0.8/0.7/0.65 → high 档 (total=3); 0.65 无前向收益 → evaluated 只算 0.8/0.7 = 2; 0.5 → mid
        assert result.by_tier["high"]["total"] == 3
        assert result.by_tier["high"]["evaluated"] == 2
        assert result.by_tier["mid"]["total"] == 1

    def test_forward_returns_passthrough(self):
        """forward_returns dict 供 ops 喂给 Agent 层 Evaluator (AG-01 闭环)"""
        conn = _make_db()
        self._seed(conn)
        result = AgentEvaluator(_Db(conn)).evaluate("2026-08-07")
        assert result.forward_returns["000001"] == pytest.approx(0.0192)

    def test_empty_date(self):
        conn = _make_db()
        result = AgentEvaluator(_Db(conn)).evaluate("2026-01-01")
        assert result.total_predictions == 0
        assert result.hit_rate is None  # 不可评估, 不是 0

    def test_no_db(self):
        result = AgentEvaluator(None).evaluate("2026-08-07")
        assert result.total_predictions == 0
        assert result.hit_rate is None


class TestFindEvaluableDate:
    def test_finds_latest_with_fwd_data(self):
        conn = _make_db()
        # 08-10 有未结算预测但 fwd 未到期 (NULL) → 不可评估
        conn.execute(
            "INSERT INTO agent_predictions (agent_name, code, date, verdict) "
            "VALUES ('quick_scan', '000001', '2026-08-10', '推荐')")
        conn.execute(
            "INSERT INTO factor_daily VALUES ('000001', '2026-08-10', NULL)")
        # 08-07 有未结算预测且 fwd 已到期 → 可评估
        conn.execute(
            "INSERT INTO agent_predictions (agent_name, code, date, verdict) "
            "VALUES ('quick_scan', '000002', '2026-08-07', '推荐')")
        conn.execute(
            "INSERT INTO factor_daily VALUES ('000002', '2026-08-07', 1.5)")
        ev = AgentEvaluator(_Db(conn))
        assert ev.find_evaluable_date() == "2026-08-07"

    def test_settled_predictions_not_re_evaluated(self):
        conn = _make_db()
        conn.execute(
            "INSERT INTO agent_predictions "
            "(agent_name, code, date, verdict, hit) "
            "VALUES ('quick_scan', '000001', '2026-07-17', '推荐', 1)")
        conn.execute(
            "INSERT INTO factor_daily VALUES ('000001', '2026-07-17', 2.0)")
        assert AgentEvaluator(_Db(conn)).find_evaluable_date() is None

    def test_no_db(self):
        assert AgentEvaluator(None).find_evaluable_date() is None

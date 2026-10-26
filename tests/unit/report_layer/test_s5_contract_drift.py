"""F5.5 契约漂移回归测试 — 用真实表列名/真实 dataclass 构造数据

修复点:
1. evening_report ExitQualitySection: t/return_pct → check_point/price_change_since_exit
2. exit_review: days_held → holding_days_actual; get_recent 幽灵方法 → get_tracking
3. ic_weight: factor_evaluations(不存在) → factor_ic (calc_date/factor_name/mean_ic)
4. overnight_market: 涨绿跌红 → A股惯例涨红跌绿
"""
from unittest.mock import MagicMock

from execution_layer.stats.display import ExitReview
from report_layer.reports.base import ReportContext
from report_layer.reports.evening_report import ExitQualitySection
from report_layer.sections.exit_review import ExitReviewSection
from report_layer.sections.ic_weight import ICWeightSection
from report_layer.sections.overnight_market import OvernightMarketSection


class TestExitQualitySectionKeys:
    """post_exit_tracking 真实列名: check_point / price_change_since_exit"""

    def _ctx_with_reviews(self, post_rows):
        el = MagicMock()
        el.get_exit_reviews.return_value = [
            ExitReview(code="001232", name="嘉立创", entry_price=170.7,
                       exit_price=161.27, total_return_pct=-5.52,
                       holding_days=2, exit_reason="止损", post_exit_t=post_rows)
        ]
        return ReportContext(date="2026-08-17", execution_layer=el)

    def test_renders_check_point_and_price_change(self):
        # 真实 get_tracking 返回 dict 列表, 键为 post_exit_tracking 列名
        post = [
            {"check_point": "T+5", "price_change_since_exit": -0.032, "quality": "correct"},
            {"check_point": "T+10", "price_change_since_exit": None, "quality": None},
        ]
        result = ExitQualitySection().build(self._ctx_with_reviews(post))
        assert result is not None
        assert "T+5" in result.body
        assert "-3.2%" in result.body
        # 未判定的 T+10 不渲染
        assert "T+10" not in result.body
        # 旧键名不应出现
        assert "return_pct" not in result.body

    def test_no_post_data_still_renders_base_line(self):
        result = ExitQualitySection().build(self._ctx_with_reviews([]))
        assert "001232" in result.body
        assert "-5.5%" in result.body

    def test_empty_reviews(self):
        el = MagicMock()
        el.get_exit_reviews.return_value = []
        result = ExitQualitySection().build(
            ReportContext(date="2026-08-17", execution_layer=el))
        assert "暂无退出记录" in result.body


class TestExitReviewSectionColumns:
    """stock_pool 真实列名: holding_days_actual / net_return_pct"""

    def _ctx(self, pool_rows, tracker=None):
        el = MagicMock()
        el.stock_pool.get_by_status.return_value = pool_rows
        el.post_exit_tracker = tracker
        # _get_post_exit_tracker 走 getattr(execution_layer, 'post_exit_tracker')
        return ReportContext(date="2026-08-17", execution_layer=el)

    def test_holding_days_actual_used(self):
        rows = [{"code": "001232", "name": "嘉立创", "entry_date": "2026-08-13",
                 "exit_date": "2026-08-15", "net_return_pct": -5.52,
                 "holding_days_actual": 2, "exit_reason": "止损"}]
        result = ExitReviewSection().build(self._ctx(rows))
        assert "持有: 2天" in result.body
        assert "-5.52%" in result.body

    def test_fallback_total_return_pct(self):
        rows = [{"code": "002543", "name": "万和电气", "entry_date": "2026-08-13",
                 "exit_date": "2026-08-15", "net_return_pct": None,
                 "total_return_pct": 4.95, "holding_days_actual": 2}]
        result = ExitReviewSection().build(self._ctx(rows))
        assert "+4.95%" in result.body

    def test_post_exit_uses_get_tracking_not_get_recent(self):
        """回归: PostExitTracker 无 get_recent, 必须走 get_tracking"""
        tracker = MagicMock(spec=["get_tracking"])  # spec 限定: 调 get_recent 会 AttributeError
        tracker.get_tracking.return_value = [
            {"check_point": "T+5", "price_change_since_exit": -0.06, "quality": "correct"},
        ]
        rows = [{"code": "001232", "name": "嘉立创", "exit_date": "2026-08-15",
                 "net_return_pct": -5.52, "holding_days_actual": 2}]
        result = ExitReviewSection().build(self._ctx(rows, tracker))
        assert "退出后表现" in result.body
        assert "T+5" in result.body
        assert "-6.0%" in result.body

    def test_post_exit_no_judged_no_section(self):
        tracker = MagicMock(spec=["get_tracking"])
        tracker.get_tracking.return_value = [
            {"check_point": "T+5", "price_change_since_exit": None, "quality": None},
        ]
        rows = [{"code": "001232", "name": "嘉立创", "exit_date": "2026-08-15",
                 "net_return_pct": -5.52, "holding_days_actual": 2}]
        result = ExitReviewSection().build(self._ctx(rows, tracker))
        assert "退出后表现" not in result.body


class TestICWeightSectionTable:
    """真实表 factor_ic (calc_date/factor_name/mean_ic), factor_evaluations 不存在"""

    def test_reads_factor_ic_table(self):
        from common.database import Database
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("""CREATE TABLE factor_ic (
            calc_date TEXT, factor_name TEXT, mean_ic REAL,
            std_ic REAL, icir REAL, win_rate REAL, effectiveness REAL,
            sample_size INTEGER, created_at TEXT)""")
        conn.execute("INSERT INTO factor_ic (calc_date, factor_name, mean_ic) VALUES "
                     "('2026-08-14', 'momentum_20d', 0.052), "
                     "('2026-08-14', 'value_discount', -0.031), "
                     "('2026-08-13', 'momentum_20d', 0.099)")
        conn.commit()
        dl = MagicMock()
        dl.db = db
        # spec=[] 使 ic_feedback_tuner/_ic_tuner getattr 返回 None → 走 DB 备用路径
        sl = MagicMock(spec=[])
        result = ICWeightSection().build(
            ReportContext(date="2026-08-17", data_layer=dl, strategy_layer=sl))
        assert "动量(20日)" in result.body
        assert "+0.052" in result.body
        # 只取最新 calc_date, 旧日期 0.099 不应出现
        assert "0.099" not in result.body

    def test_no_table_graceful(self):
        from common.database import Database
        db = Database(":memory:")
        dl = MagicMock()
        dl.db = db
        sl = MagicMock(spec=[])
        result = ICWeightSection().build(
            ReportContext(date="2026-08-17", data_layer=dl, strategy_layer=sl))
        assert "暂不可用" in result.body


class TestOvernightMarketColors:
    """A股惯例: 涨红(🔴) 跌绿(🟢)"""

    def test_up_red_down_green(self):
        section = OvernightMarketSection()
        section._fetch_overnight = lambda: {
            "dow": 45000.0, "dow_pct": 1.2,
            "nasdaq": 21000.0, "nasdaq_pct": -0.8,
        }
        result = section.build(ReportContext(date="2026-08-17"))
        lines = result.body.split("\n")
        dow_line = next(l for l in lines if "道琼斯" in l)
        nas_line = next(l for l in lines if "纳斯达克" in l)
        assert "🔴" in dow_line  # 涨 → 红
        assert "🟢" in nas_line  # 跌 → 绿

    def test_empty_data_graceful(self):
        section = OvernightMarketSection()
        section._fetch_overnight = lambda: {}
        result = section.build(ReportContext(date="2026-08-17"))
        assert "暂不可用" in result.body

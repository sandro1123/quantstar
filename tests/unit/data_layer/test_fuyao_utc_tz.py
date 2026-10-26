"""财务修法① (fin_source_card 病A): fuyao period/announce 时间戳 BJT 渲染回归测试。

病根: fuyao.py 三处 `datetime.fromtimestamp(ms/1000, tz=UTC)` 用 UTC 渲染
period_end_ms / report_date_ms。fuyao 时间戳语义 = BJT 毫秒戳 (实锭:
period_end_ms=1782748800000 = 2026-06-30 00:00 BJT 即 Q2 季末零点;
probe2 P2 实测 report_date_ms=1786723200000 = 2026-08-15 00:00 BJT 公告日零点)。
UTC 渲染把 06-30 零点 BJT 落成 06-29 → Q2 全批 date 键差一天
(stock_financials 现存 2026-06-29 批 5546 行 / 2026-03-30 批 5193 行实证)。

修法: tz=UTC → tz=BJT (timezone(timedelta(hours=8)), 市场本地时间)。

红-绿: 撤回本修法 (还原三处 tz=UTC) 时本文件必须失败。
测试不依赖 fuyao 源可用 (mocked client + 内存库, fixture 自持)。
"""
from datetime import datetime
from unittest.mock import MagicMock

from data_layer.pipelines.fuyao import FuyaoPipeline, _report_period_date

# 实锭常量 (fuyao 真实返回语义 = BJT):
Q2_END_MS = 1782748800000    # 2026-06-30 00:00 BJT (Q2 季末零点) — UTC 渲染病键 06-29
Q1_END_MS = 1774886400000    # 2026-03-31 00:00 BJT (Q1 季末零点) — UTC 渲染病键 03-30
ANNOUNCE_MS = 1786723200000  # 2026-08-15 00:00 BJT (probe2 P2 实测返回) — UTC 渲染 08-14
Q3_FUTURE_MS = 1790697600000 # 2026-09-30 00:00 BJT (Q3 季末, 未来期)


class TestPeriodDateBJT:
    """_report_period_date 必须渲染为市场本地日 (BJT), 非 UTC 日。"""

    def _client_with(self, period_ms):
        client = MagicMock()
        client._get.return_value = {
            "item": [{"period_end_ms": period_ms}]
        }
        return client

    def test_q2_period_end_renders_june_30_not_29(self):
        """实锭对拍: fuyao 真实返回 1782748800000 → 2026-06-30 (非 06-29 病键)。"""
        got = _report_period_date(self._client_with(Q2_END_MS), "2026-2")
        assert got == "2026-06-30", (
            f"period_end_ms={Q2_END_MS} 渲染为 {got}, 应为 BJT 2026-06-30。"
            "tz=UTC 会落 06-29 病键 (现库 Q2 批 5546 行实证)"
        )

    def test_q1_period_end_renders_calendar_quarter_end(self):
        """Q1 季末零点 BJT → 2026-03-31 (非现库 03-30 病键)。"""
        got = _report_period_date(self._client_with(Q1_END_MS), "2026-1")
        assert got == "2026-03-31"

    def test_midnight_boundary_is_exact(self):
        """BJT 零点临界: 00:00 BJT 落当日, 不前移一天。"""
        got = _report_period_date(self._client_with(Q2_END_MS), "2026-2")
        assert datetime.strptime(got, "%Y-%m-%d") is not None
        assert got == "2026-06-30"


class TestPipelineWritesBJTKeys:
    """FuyaoPipeline 主写入路径: date 键与 announce_date 都必须 BJT 渲染。"""

    def _make_fixture(self):
        """内存库 + 单只股票 fixture (零网络零生产库)。"""
        import sqlite3
        from data_layer.pipelines.base import PipelineInput

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE stock_info (code TEXT PRIMARY KEY, is_active INTEGER)")
        conn.execute("INSERT INTO stock_info VALUES ('000001', 1)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_financials (
                code TEXT, date TEXT, eps REAL DEFAULT 0, roe REAL DEFAULT 0,
                revenue REAL DEFAULT 0, profit REAL DEFAULT 0,
                total_assets REAL DEFAULT 0, total_liabilities REAL DEFAULT 0,
                net_assets REAL DEFAULT 0, operating_cash_flow REAL DEFAULT 0,
                gross_margin REAL DEFAULT 0, net_margin REAL DEFAULT 0,
                announce_date TEXT DEFAULT '', caliber TEXT DEFAULT 'cumulative',
                PRIMARY KEY (code, date)
            )
        """)
        conn.commit()

        class _DB:
            def __init__(self, c):
                self._c = c

            def get_connection(self):
                return self._c

        db = _DB(conn)
        inp = PipelineInput(date="2026-09-08", db=db)
        return conn, db, inp

    def _client(self, period_ms, announce_ms):
        """mock FuyaoClient — 显式设具名方法 (管线经它们取数)。"""
        fake = MagicMock()
        fake.api_key = "sk-test"
        fake.available.return_value = True
        fake.get_income_statements.return_value = [{
            "period_end_ms": period_ms,
            "report_date_ms": announce_ms,
            "basic_eps": 1.23, "operating_income": 70.6e9,
            "parent_holder_net_profit": 30.8e9,
        }]
        fake.get_balance_sheets.return_value = [{
            "assets_total": 5e12, "total_debt": 4e12,
            "holder_equity_total": 1e12,
        }]
        fake.get_cash_flow.return_value = [{"act_cash_flow_net": 1e11}]
        fake.get_financial_indicators.return_value = {
            "abilities": [{"ability": "profitability", "indicators": [
                {"index_id": "index_weighted_avg_roe", "value": 10.5},
                {"index_id": "sale_gross_margin", "value": 20.0},
                {"index_id": "sale_net_interest_ratio", "value": 8.0},
            ]}]
        }
        # _detect_best_report / _report_period_date 走 client._get —
        # income 探测端点返回带 period_end_ms 的 item
        fake._get.return_value = {"item": [{"period_end_ms": period_ms}]}
        return fake

    def test_q2_row_lands_june_30_key(self):
        """端到端 (mock client): Q2 批必须落 date=2026-06-30, announce=2026-08-15。

        撤回修法时 (tz=UTC): date 落 06-29 → 本测 RED。
        """
        conn, db, inp = self._make_fixture()
        pipe = FuyaoPipeline()
        pipe._client = self._client(Q2_END_MS, ANNOUNCE_MS)
        pipe.run(inp)

        row = conn.execute(
            "SELECT date, announce_date, eps FROM stock_financials WHERE code='000001'"
        ).fetchone()
        assert row is not None, "管线零写入 — fixture 链断了, 非修法断言面"
        assert row["date"] == "2026-06-30", (
            f"Q2 批落键 {row['date']} 非 2026-06-30 (UTC off-by-one 病在)"
        )
        assert row["announce_date"] == "2026-08-15", (
            f"announce_date={row['announce_date']} 非 BJT 2026-08-15"
        )
        assert row["eps"] == 1.23

    def test_no_future_period_rows_written(self):
        """period_date > input.date 的行必须跳过 — BJT 修法不得破坏该守卫。"""
        conn, db, inp = self._make_fixture()
        pipe = FuyaoPipeline()
        pipe._client = self._client(Q3_FUTURE_MS, ANNOUNCE_MS)
        pipe.run(inp)

        n = conn.execute("SELECT COUNT(*) FROM stock_financials").fetchone()[0]
        assert n == 0, "未来报告期行不应写入 (守卫被 BJT 修法破坏)"

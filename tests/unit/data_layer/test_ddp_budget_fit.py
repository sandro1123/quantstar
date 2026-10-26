"""DDP-BUDGET-FIT: 北交所短路 + D-43 熔断 + analyst_forecasts 超时配置断言

08-28 daily_data_pipeline 两次翻车根因修复验证:
① kline 北交所旧码 (43/83/87) 批量短路 — 全源无覆盖不逐股×4源空跑
② 东财端点连续失败熔断 — 反爬全面封锁时不逐码空跑
③ analyst_forecasts 子超时 1200→1900 — 预算 kline≤300+其余~333+1900≤2600
"""
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from common.database import Database
from data_layer.pipelines.kline import KlinePipeline, _BSE_SKIP_PREFIXES
from data_layer.pipelines.base import PipelineInput
from data_layer.sources.akshare_source import AkShareSourceAdapter


class RecordingSource:
    """记录被请求的 code, 返回空数据 (模拟全源无覆盖)"""
    name = "recorder"
    capabilities = {"kline"}

    def __init__(self):
        self.requested_codes = []

    def is_available(self):
        return True

    def get_data(self, dtype, params):
        self.requested_codes.append(params.get("code", ""))
        return []


class RecordingRegistry:
    def __init__(self, source):
        self._source = source

    def get_sources_for(self, dtype):
        if dtype == "kline_bulk":
            return []  # 无 bulk 源 → 走逐股路径
        return [self._source]


def _make_db_with_stocks(codes):
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT PRIMARY KEY, name TEXT NOT NULL,
            industry TEXT DEFAULT '', market TEXT DEFAULT 'SZ',
            list_date TEXT DEFAULT '', is_active INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, amount REAL, pre_close REAL,
            change_pct REAL, turnover_rate REAL,
            PRIMARY KEY (code, date)
        )
    """)
    for c in codes:
        conn.execute(
            "INSERT INTO stock_info (code, name, is_active) VALUES (?, ?, 1)",
            (c, f"stock_{c}"))
    conn.commit()
    return db


class TestKlineBseSkip:
    """① 北交所旧码批量短路"""

    def test_bse_prefixes_defined(self):
        assert _BSE_SKIP_PREFIXES == ("43", "83", "87")

    def test_full_market_skips_bse_old_codes(self):
        """全市场模式: 43/83/87 码不发起拉取, 92/00/60 码正常拉取"""
        codes = ["430017", "830001", "870001", "920001", "000001", "600519"]
        db = _make_db_with_stocks(codes)
        source = RecordingSource()
        pipeline = KlinePipeline(source_registry=RecordingRegistry(source))
        inp = PipelineInput(date="2026-08-28", start_date="2026-08-28",
                            end_date="2026-08-28", db=db, symbols=[])
        pipeline.run(inp)
        requested = set(source.requested_codes)
        # 北交所旧码不得被请求
        assert "430017" not in requested
        assert "830001" not in requested
        assert "870001" not in requested
        # 92xxx (有覆盖) + 沪深码正常请求
        assert "920001" in requested
        assert "000001" in requested
        assert "600519" in requested

    def test_explicit_symbols_not_filtered(self):
        """指定符号集模式: 不干预, 即使含北交所码也照常拉取"""
        codes = ["430017", "000001"]
        db = _make_db_with_stocks(codes)
        source = RecordingSource()
        pipeline = KlinePipeline(source_registry=RecordingRegistry(source))
        inp = PipelineInput(date="2026-08-28", start_date="2026-08-28",
                            end_date="2026-08-28", db=db, symbols=["430017"])
        pipeline.run(inp)
        assert "430017" in source.requested_codes


class TestEmCircuitBreaker:
    """② 东财端点连续失败熔断"""

    def test_circuit_breaker_trips_after_threshold(self):
        """连续失败 20 次后熔断, 后续调用不发 HTTP"""
        adapter = AkShareSourceAdapter()
        mock_ak = MagicMock()
        mock_ak.stock_research_report_em.side_effect = KeyError("infoCode")

        # 用真实 _em_call 逻辑, 计数实际 HTTP 调用次数
        for i in range(30):
            try:
                result = adapter._em_call(mock_ak, "stock_research_report_em", symbol="000001")
            except KeyError:
                pass  # 熔断前: 真正调用并抛异常

        # 前 20 次真正调用 (抛异常), 后 10 次被熔断拦截 (返回 None 不调用)
        assert mock_ak.stock_research_report_em.call_count == AkShareSourceAdapter._EM_CIRCUIT_THRESHOLD

    def test_circuit_breaker_resets_on_success(self):
        """成功一次重置计数, 不触发熔断"""
        adapter = AkShareSourceAdapter()
        mock_ak = MagicMock()
        import pandas as pd
        mock_ak.stock_research_report_em.side_effect = [
            KeyError("infoCode"),  # fail 1
            KeyError("infoCode"),  # fail 2
            pd.DataFrame({"col": [1]}),  # success → reset
            KeyError("infoCode"),  # fail 1 (reset 后重新计数)
        ]
        # 前两次失败
        try:
            adapter._em_call(mock_ak, "stock_research_report_em", symbol="a")
        except KeyError:
            pass
        try:
            adapter._em_call(mock_ak, "stock_research_report_em", symbol="b")
        except KeyError:
            pass
        # 成功 → 重置
        result = adapter._em_call(mock_ak, "stock_research_report_em", symbol="c")
        assert result is not None
        assert adapter._em_consec_fail.get("stock_research_report_em") is None
        # 再失败 → 重新计数从 1 开始
        try:
            adapter._em_call(mock_ak, "stock_research_report_em", symbol="d")
        except KeyError:
            pass
        assert adapter._em_consec_fail.get("stock_research_report_em") == 1

    def test_no_circuit_below_threshold(self):
        """连续失败 < 阈值时不熔断, 继续调用"""
        adapter = AkShareSourceAdapter()
        mock_ak = MagicMock()
        mock_ak.stock_research_report_em.side_effect = KeyError("infoCode")
        for _ in range(10):
            try:
                adapter._em_call(mock_ak, "stock_research_report_em", symbol="x")
            except KeyError:
                pass
        assert mock_ak.stock_research_report_em.call_count == 10
        assert adapter._em_consec_fail["stock_research_report_em"] == 10


class TestAnalystForecastTimeout:
    """③ analyst_forecasts 超时配置断言 (DDP c-1 后: 独立 16:55 顶层管道预算)"""

    def test_analyst_forecasts_timeout_is_1900(self):
        """_capability_timeout["analyst_forecasts"] == 1900 — c-1 后语义:
        独立管道预算, 与 ops.yaml analyst_forecasts.timeout_s 1900 双钉对齐"""
        import data_layer
        import inspect
        src = inspect.getsource(data_layer.DataLayer.run_pipeline)
        assert '"analyst_forecasts": 1900' in src

    def test_budget_fits_within_2700(self):
        """预算论证 (c-1 拆分后 DDP 组成): kline≤300 + 其余~333 ≤ 2600 < 2700。
        analyst_forecasts 已拆出 (16:55 独立顶层, 自身 1900 不再占 DDP 串行),
        三天实测 DDP 串行 2738→908s / 3223→1393s 全回预算内 (设计稿 §4c-1)。"""
        kline_max = 300
        other_max = 333  # stock_list+fuyao+valuation+moneyflow+concept+lhb+sentiment+news+limit_up+shortterm+index
        total = kline_max + other_max
        assert total <= 2600, f"预算超支: {total} > 2600"
        assert total < 2700, f"撞外层预算: {total} >= 2700"
        # c-1 拆分语义防回退锚 (regex 读源, 同 test_concept_intervals:362 先例):
        # data_layer 分片下兄弟测试 (test_news_attribution_*) sys.path.insert
        # 他树 src → 函数体内 import ops_layer 会拿到污染副本; 源文件断言免疫。
        _orch = (Path(__file__).resolve().parents[3]
                 / "src" / "ops_layer" / "orchestrator.py").read_text(encoding="utf-8")
        _m = re.search(r"DAILY_SUB_PIPELINES\s*=\s*\[(.*?)\]", _orch, re.S)
        assert _m, "orchestrator DAILY_SUB_PIPELINES 常量未找到"
        assert "analyst_forecasts" not in _m.group(1), \
            "c-1 防回退锚: analyst_forecasts 不得回 DAILY_SUB_PIPELINES"

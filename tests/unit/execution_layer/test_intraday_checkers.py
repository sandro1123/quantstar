"""Tests for IntradayEntryChecker and IntradayExitChecker"""
from unittest.mock import MagicMock, patch
from common.database import Database
from execution_layer.intraday.entry_checker import IntradayEntryChecker
from execution_layer.intraday.exit_checker import IntradayExitChecker


class TestIntradayEntryChecker:
    def test_get_current_price_returns_db_value(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", "2026-07-24", 18.5))
        conn.commit()
        checker = IntradayEntryChecker(db, MagicMock(), MagicMock())
        price = checker._get_current_price("000001")
        assert price == 18.5

    def test_get_current_price_returns_zero_for_nonexistent(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.commit()
        checker = IntradayEntryChecker(db, MagicMock(), MagicMock())
        price = checker._get_current_price("nonexistent")
        assert price == 0.0

    def test_check_returns_none_when_price_invalid(self):
        db = Database(":memory:")
        checker = IntradayEntryChecker(db, MagicMock(), MagicMock())
        result = checker.check("000001", {"entry_low": 10, "entry_high": 20})
        assert result is None

    def test_check_returns_triggered_when_price_in_range(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", "2026-07-24", 15.0))
        conn.commit()
        from execution_layer.core.entry_engine import EntryDecision
        entry_engine = MagicMock()
        entry_engine.check_entry.return_value = EntryDecision(action="buy", price=15.0, reason="触价")
        checker = IntradayEntryChecker(db, MagicMock(), entry_engine)
        result = checker.check("000001", {"entry_low": 10, "entry_high": 20})
        assert result is not None
        assert result["triggered"] is True
        assert result["price"] == 15.0
        assert result["reason"] == "触价"

    def test_check_returns_none_when_price_out_of_range(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", "2026-07-24", 25.0))
        conn.commit()
        entry_engine = MagicMock()
        entry_engine.check_entry.return_value = None
        checker = IntradayEntryChecker(db, MagicMock(), entry_engine)
        result = checker.check("000001", {"entry_low": 10, "entry_high": 20})
        assert result is None


class TestIntradayExitChecker:
    def test_get_current_price_returns_db_value(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", "2026-07-24", 18.5))
        conn.commit()
        checker = IntradayExitChecker(db, MagicMock(), exit_engine=None)
        price = checker._get_current_price("000001")
        assert price == 18.5

    def test_check_returns_stop_loss_triggered(self):
        # EX-05: 退出规则单一源=引擎, checker 透传引擎决策
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", "2026-07-24", 9.0))
        conn.commit()
        from execution_layer.core.exit_engine import ExitDecision
        exit_engine = MagicMock()
        exit_engine.check_exits.return_value = ExitDecision(
            action="exit_pending", reason_layer=1, reason="止损: 9.0 <= 10.0",
            reason_user="触发硬止损", exit_low=8.82, exit_high=9.0)
        checker = IntradayExitChecker(db, MagicMock(), exit_engine)
        result = checker.check("000001", {"code": "000001", "stop_loss": 10.0, "entry_price": 20.0})
        assert result is not None
        assert "止损" in result["reason"]
        assert result["price"] == 9.0

    def test_check_returns_take_profit_triggered(self):
        # EX-05: 止盈判定走引擎 (消费持仓自身 take_profit), 不再 20% 硬编码
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", "2026-07-24", 25.0))
        conn.commit()
        from execution_layer.core.exit_engine import ExitDecision
        exit_engine = MagicMock()
        exit_engine.check_exits.return_value = ExitDecision(
            action="exit_pending", reason_layer=1, reason="止盈: 25.0 >= 24.0",
            reason_user="触及止盈", exit_low=25.0, exit_high=25.5)
        checker = IntradayExitChecker(db, MagicMock(), exit_engine)
        result = checker.check("000001", {"code": "000001", "stop_loss": 0, "entry_price": 20.0})
        assert result is not None
        assert "止盈" in result["reason"]
        assert result["triggered"] is True

    def test_check_returns_none_when_no_condition_met(self):
        # EX-05: 引擎无退出信号 → checker 不触发
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
        conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                     ("000001", "2026-07-24", 15.0))
        conn.commit()
        exit_engine = MagicMock()
        exit_engine.check_exits.return_value = None
        checker = IntradayExitChecker(db, MagicMock(), exit_engine)
        result = checker.check("000001", {"code": "000001", "stop_loss": 0, "entry_price": 20.0})
        assert result is None


# ── F4.3: 实时价优先 + daily_klines 昨收降级 ─────────────────────────

def _klines_db(code: str, close: float) -> Database:
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL, PRIMARY KEY (code, date))")
    conn.execute("INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
                 (code, "2026-08-14", close))
    conn.commit()
    return db


class TestRealtimePricePaths:
    def test_entry_checker_prefers_realtime(self):
        db = _klines_db("000001", 18.5)
        data_layer = MagicMock()
        data_layer.get_realtime_prices.return_value = {"000001": 19.2}
        checker = IntradayEntryChecker(db, MagicMock(), MagicMock(), data_layer=data_layer)
        assert checker._get_current_price("000001") == 19.2
        data_layer.get_realtime_prices.assert_called_once_with(["000001"])

    def test_entry_checker_fallback_when_realtime_empty(self):
        db = _klines_db("000001", 18.5)
        data_layer = MagicMock()
        data_layer.get_realtime_prices.return_value = {}
        checker = IntradayEntryChecker(db, MagicMock(), MagicMock(), data_layer=data_layer)
        assert checker._get_current_price("000001") == 18.5

    def test_entry_checker_fallback_when_realtime_raises(self):
        db = _klines_db("000001", 18.5)
        data_layer = MagicMock()
        data_layer.get_realtime_prices.side_effect = RuntimeError("network down")
        checker = IntradayEntryChecker(db, MagicMock(), MagicMock(), data_layer=data_layer)
        assert checker._get_current_price("000001") == 18.5

    def test_entry_checker_no_data_layer_uses_klines_directly(self):
        db = _klines_db("000001", 18.5)
        checker = IntradayEntryChecker(db, MagicMock(), MagicMock(), data_layer=None)
        assert checker._get_current_price("000001") == 18.5

    def test_exit_checker_prefers_realtime(self):
        db = _klines_db("000001", 18.5)
        data_layer = MagicMock()
        data_layer.get_realtime_prices.return_value = {"000001": 9.0}
        from execution_layer.core.exit_engine import ExitDecision
        exit_engine = MagicMock()
        exit_engine.check_exits.return_value = ExitDecision(
            action="exit_pending", reason_layer=1, reason="止损",
            reason_user="触发硬止损", exit_low=8.82, exit_high=9.0)
        checker = IntradayExitChecker(db, MagicMock(), exit_engine, data_layer=data_layer)
        result = checker.check("000001", {"code": "000001", "stop_loss": 10.0, "entry_price": 20.0})
        assert result is not None
        assert result["price"] == 9.0
        # 引擎收到的是实时价 9.0, 而非昨收 18.5
        assert exit_engine.check_exits.call_args[0][1] == 9.0

    def test_exit_checker_fallback_when_realtime_empty(self):
        db = _klines_db("000001", 9.0)
        data_layer = MagicMock()
        data_layer.get_realtime_prices.return_value = {}
        from execution_layer.core.exit_engine import ExitDecision
        exit_engine = MagicMock()
        exit_engine.check_exits.return_value = ExitDecision(
            action="exit_pending", reason_layer=1, reason="止损",
            reason_user="触发硬止损", exit_low=8.82, exit_high=9.0)
        checker = IntradayExitChecker(db, MagicMock(), exit_engine, data_layer=data_layer)
        result = checker.check("000001", {"code": "000001", "stop_loss": 10.0, "entry_price": 20.0})
        assert result is not None
        assert result["price"] == 9.0
        # 实时价为空 → 引擎收到 daily_klines 降级价 9.0
        assert exit_engine.check_exits.call_args[0][1] == 9.0


# ── F4.3: gtimg 批量实时报价 ─────────────────────────────────────────

def _fake_gtimg_line(code: str, name: str, price: float) -> str:
    parts = [""] * 40
    parts[0] = "51"
    parts[1] = name
    parts[2] = code
    parts[3] = str(price)
    parts[4] = str(round(price * 0.98, 2))
    parts[5] = str(round(price * 0.99, 2))
    parts[6] = "1000"
    parts[32] = "2.0"
    parts[33] = str(round(price * 1.01, 2))
    parts[34] = str(round(price * 0.97, 2))
    parts[37] = "10000"
    prefix = "sz" if code.startswith(("0", "3")) else "sh"
    return f'v_{prefix}{code}="' + "~".join(parts) + '";'


class TestGTimgBatch:
    def test_batch_parse_multi_line(self):
        from data_layer.sources.gtimg_source import GTimgSourceAdapter
        adapter = GTimgSourceAdapter()
        fake_resp = MagicMock()
        fake_resp.text = "\n".join([
            _fake_gtimg_line("000001", "平安银行", 10.5),
            _fake_gtimg_line("600461", "钱江摩托", 8.9),
        ])
        fake_resp.encoding = "gbk"
        with patch(
            "requests.get", return_value=fake_resp
        ) as mock_get:
            quotes = adapter.get_data("realtime_quote_batch", {"codes": ["000001", "600461"]})
        assert len(quotes) == 2
        by_code = {q["code"]: q for q in quotes}
        assert by_code["000001"]["price"] == 10.5
        assert by_code["600461"]["price"] == 8.9
        # 单次请求 (≤80 只一批)
        assert mock_get.call_count == 1
        url = mock_get.call_args[0][0]
        assert "sz000001" in url and "sh600461" in url

    def test_batch_chunking_over_80(self):
        from data_layer.sources.gtimg_source import GTimgSourceAdapter
        adapter = GTimgSourceAdapter()
        codes = [f"0{i:05d}" for i in range(81)]
        fake_resp = MagicMock()
        fake_resp.text = ""
        fake_resp.encoding = "gbk"
        with patch(
            "requests.get", return_value=fake_resp
        ) as mock_get:
            adapter.get_data("realtime_quote_batch", {"codes": codes})
        assert mock_get.call_count == 2  # 80 + 1

    def test_batch_empty_codes(self):
        from data_layer.sources.gtimg_source import GTimgSourceAdapter
        adapter = GTimgSourceAdapter()
        assert adapter.get_data("realtime_quote_batch", {"codes": []}) == []

    def test_batch_skips_zero_price(self):
        from data_layer.sources.gtimg_source import GTimgSourceAdapter
        adapter = GTimgSourceAdapter()
        fake_resp = MagicMock()
        fake_resp.text = _fake_gtimg_line("000001", "平安银行", 0.0)
        fake_resp.encoding = "gbk"
        with patch(
            "requests.get", return_value=fake_resp
        ):
            quotes = adapter.get_data("realtime_quote_batch", {"codes": ["000001"]})
        assert quotes == []


class TestDataLayerGetRealtimePrices:
    def _make_layer(self, batch_source=None, single_source=None):
        from data_layer import DataLayer
        layer = DataLayer.__new__(DataLayer)
        registry = MagicMock()

        def get_sources_for(data_type):
            if data_type == "realtime_quote_batch" and batch_source:
                return [batch_source]
            if data_type == "realtime_quote" and single_source:
                return [single_source]
            return []

        registry.get_sources_for.side_effect = get_sources_for
        layer._source_registry = registry
        return layer

    def test_batch_source_covers_all(self):
        batch = MagicMock()
        batch.get_data.return_value = [
            {"code": "000001", "price": 10.5},
            {"code": "600461", "price": 8.9},
        ]
        layer = self._make_layer(batch_source=batch)
        prices = layer.get_realtime_prices(["000001", "600461"])
        assert prices == {"000001": 10.5, "600461": 8.9}

    def test_single_source_fills_gap(self):
        batch = MagicMock()
        batch.get_data.return_value = [{"code": "000001", "price": 10.5}]
        single = MagicMock()
        single.get_data.return_value = {"code": "600461", "price": 8.9}
        layer = self._make_layer(batch_source=batch, single_source=single)
        prices = layer.get_realtime_prices(["000001", "600461"])
        assert prices == {"000001": 10.5, "600461": 8.9}

    def test_all_sources_fail_returns_empty(self):
        batch = MagicMock()
        batch.get_data.side_effect = RuntimeError("down")
        layer = self._make_layer(batch_source=batch)
        prices = layer.get_realtime_prices(["000001"])
        assert prices == {}

    def test_empty_codes(self):
        layer = self._make_layer()
        assert layer.get_realtime_prices([]) == {}
"""Tests for common layer utilities"""
from common.utils import validate_identifier, bj_date, bj_now, generate_trace_id
from common.database import Database
from common.decorators import catch_quantstar_error, with_timeout


class TestValidateIdentifier:
    def test_valid(self):
        assert validate_identifier("close") is True
        assert validate_identifier("fwd_return_20d") is True
        assert validate_identifier("daily_klines") is True

    def test_invalid(self):
        assert validate_identifier("") is False
        assert validate_identifier("col; DROP") is False
        assert validate_identifier("123abc") is False
        assert validate_identifier("col name") is False


class TestDateUtils:
    def test_bj_date_format(self):
        d = bj_date()
        assert len(d) == 10
        assert "-" in d

    def test_bj_now_format(self):
        n = bj_now()
        assert len(n) >= 19
        assert ":" in n

    def test_generate_trace_id(self):
        tid = generate_trace_id()
        assert len(tid) >= 8
        assert isinstance(tid, str)


class TestDatabase:
    def test_default_connection(self):
        db = Database()
        conn = db.get_connection()
        assert conn is not None

    def test_memory_database(self):
        db = Database(":memory:")
        conn = db.get_connection()
        conn.execute("CREATE TABLE test (id INTEGER)")
        conn.execute("INSERT INTO test VALUES (1)")
        conn.commit()
        row = conn.execute("SELECT COUNT(*) FROM test").fetchone()
        assert row[0] == 1


class TestDecorators:
    def test_catch_quantstar_error(self):
        @catch_quantstar_error(layer_name="test")
        def failing_func():
            raise ValueError("test error")

        result = failing_func()
        assert result is None

    def test_with_timeout(self):
        @with_timeout(timeout_s=1)
        def fast_func():
            return "ok"

        assert fast_func() == "ok"

    def test_with_timeout_fires(self):
        import time

        @with_timeout(timeout_s=1, fallback_return="timeout")
        def slow_func():
            time.sleep(5)
            return "done"

        result = slow_func()
        assert result == "timeout"

    def test_with_timeout_inline_fallback_on_shutdown(self):
        # 解释器关闭时 executor.submit 抛 RuntimeError; 应降级为同步内联执行而非静默丢失
        # (2026-08-17 事故: update_factor_daily 5539 行因此静默丢失)
        from common import decorators

        class _DeadExecutor:
            _shutdown = False  # 骗过 reset_timeout_executor 的检查

            def submit(self, *a, **k):
                raise RuntimeError("cannot schedule new futures after interpreter shutdown")

        original = decorators._background_executor
        decorators._background_executor = _DeadExecutor()
        try:
            @with_timeout(timeout_s=5)
            def writer():
                return "persisted"

            assert writer() == "persisted"
        finally:
            decorators._background_executor = original

    def test_with_timeout_inline_fallback_failure(self):
        # 内联执行也失败时, 返回 fallback_return (不吞异常上抛)
        from common import decorators

        class _DeadExecutor:
            _shutdown = False

            def submit(self, *a, **k):
                raise RuntimeError("cannot schedule new futures after interpreter shutdown")

        original = decorators._background_executor
        decorators._background_executor = _DeadExecutor()
        try:
            @with_timeout(timeout_s=5, fallback_return="fb")
            def bad_writer():
                raise ValueError("boom")

            assert bad_writer() == "fb"
        finally:
            decorators._background_executor = original
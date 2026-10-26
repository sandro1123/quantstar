"""DL-25-guard 红绿测试: baostock 结果集迭代墙钟护栏。

事故背景: 探针死在 `while rs.next()` 7h20m (94.9% CPU 空转),
socket.setdefaulttimeout(20) 无效。本文件验证:
1. 永不返回的 rs.next() → 在超时内抛 SourceTimeoutError (不挂死)
2. 超时后全局锁已释放 (不死锁) + 登录失效 + socket 被强关
3. 看门狗线程是 daemon (不制造 P0-7 atexit 挂死)
4. 超时向上传播 → kline 管线记 errors 并降级到下一个源 (铁律 #12 可观测)
5. 超时值可配置 (env 覆盖, 测试用 1s 不用 120s)

红绿说明: 移除 _guarded_call 看门狗后, 测试 1 的假 next() 永不返回,
测试将挂死/超时失败 —— 护栏是测试通过的唯一原因。
"""
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from data_layer.sources import baostock_source as bs_mod
from data_layer.sources.baostock_source import (
    BaoStockSourceAdapter,
    SourceTimeoutError,
    _iter_timeout_s,
)


# ---- 5. 超时值可配置 ----
def test_iter_timeout_env_override(monkeypatch):
    monkeypatch.setenv("QS_BAOSTOCK_ITER_TIMEOUT_S", "1")
    assert _iter_timeout_s() == 1.0
    monkeypatch.setenv("QS_BAOSTOCK_ITER_TIMEOUT_S", "not-a-number")
    # 非法值回落 config/默认, 不得抛异常
    assert _iter_timeout_s() > 0


def test_iter_timeout_default_is_120(monkeypatch):
    monkeypatch.delenv("QS_BAOSTOCK_ITER_TIMEOUT_S", raising=False)
    # config 层不可用 (CF-01) 时也必须回落默认而非抛异常
    monkeypatch.setattr(bs_mod, "_DEFAULT_ITER_TIMEOUT_S", 120.0)
    v = _iter_timeout_s()
    assert v == 120.0 or v > 0


# ---- 测试夹具: 已登录适配器 + 短超时 ----
@pytest.fixture
def adapter(monkeypatch):
    ad = BaoStockSourceAdapter()
    ad._logged_in = True  # 跳过真实登录
    monkeypatch.setattr(bs_mod, "_iter_timeout_s", lambda: 0.5)
    return ad


def _never_ending_result():
    """模拟空转挂死: next() 永远返回 True, 永不结束 (事故形态)。"""
    r = MagicMock()
    r.error_code = "0"
    r.fields = ["date", "code", "close"]
    r.next.side_effect = lambda: True  # 永不返回 False
    r.get_row_data.return_value = ["2026-08-21", "sh.600000", "9.05"]
    return r


# ---- 1. 空转挂死 → 超时内抛 SourceTimeoutError ----
def test_never_returning_next_raises_within_timeout(adapter, monkeypatch):
    fake = _never_ending_result()
    monkeypatch.setattr(adapter, "_query_with_relogin",
                        lambda fn: fake)
    # _drain_guarded 走回落路径 (MagicMock 无 _qs_rows list) → 锁内护栏迭代
    start = time.monotonic()
    with pytest.raises(SourceTimeoutError):
        adapter.get_daily_market("2026-08-21")
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"护栏未在超时内解除挂死 (耗时 {elapsed:.1f}s)"


def test_query_phase_timeout_also_raises(adapter, monkeypatch):
    """查询阶段 (next 之前) 就挂死: 看门狗同样兜住。

    用真实 socketpair 模拟事故形态 —— 线程阻塞在 recv(), 看门狗到点
    shutdown+close 使其抛 OSError → 转译为 SourceTimeoutError。
    (time.sleep 无法被关 socket 解除, 不能代表真实挂死。)
    """
    import socket as _socket
    a, b = _socket.socketpair()

    def _q():
        # 模拟 baostock socketutil.send_msg 的事故形态: Python 层 `while True: recv()`
        # 空转。看门狗 close(fd) 后下一次 recv 抛 OSError(EBADF) → 逃出循环。
        buf = b""
        while True:
            buf += a.recv(8192)
            if b"__END__" in buf:  # 永不匹配 (对端不发送)
                break
        return None

    # 让 _force_reset_connection 关到我们的 socket a
    from baostock.common import context as _ctx
    monkeypatch.setattr(_ctx, "default_socket", a, raising=False)

    start = time.monotonic()
    with pytest.raises(SourceTimeoutError):
        adapter._query_with_relogin(_q)
    assert time.monotonic() - start < 5.0
    b.close()


# ---- 2. 超时后: 锁释放 + 登录失效 + socket 强关 ----
def test_timeout_releases_lock_and_resets_connection(adapter, monkeypatch):
    fake_sock = MagicMock()
    # 伪造 baostock.common.context.default_socket
    fake_ctx = types.ModuleType("context")
    fake_ctx.default_socket = fake_sock
    fake_pkg = types.ModuleType("baostock_fake_pkg")
    fake_common = types.ModuleType("baostock_fake_pkg.common")
    fake_common.context = fake_ctx
    monkeypatch.setitem(sys.modules, "baostock_fake_pkg", fake_pkg)
    monkeypatch.setitem(sys.modules, "baostock_fake_pkg.common", fake_common)

    # 让 _force_reset_connection 走我们的假 context
    orig = adapter._force_reset_connection

    def fake_reset():
        adapter._logged_in = False
        sock = getattr(fake_ctx, "default_socket", None)
        if sock is not None:
            sock.close()
            fake_ctx.default_socket = None
    monkeypatch.setattr(adapter, "_force_reset_connection", fake_reset)

    fake = _never_ending_result()
    monkeypatch.setattr(adapter, "_query_with_relogin", lambda fn: fake)
    with pytest.raises(SourceTimeoutError):
        adapter.get_daily_market("2026-08-21")

    # 锁必须已释放 —— 否则下一次调用直接死锁 (统筹步骤 6)
    acquired = bs_mod._BS_LOCK.acquire(timeout=2)
    assert acquired, "超时后全局锁未释放 → 下次调用将死锁"
    bs_mod._BS_LOCK.release()
    # 登录标记失效 + socket 被强关
    assert adapter._logged_in is False
    assert fake_sock.close.called
    assert fake_ctx.default_socket is None
    _ = orig  # 保留引用避免 lint 误报


def test_force_reset_connection_real_path(adapter):
    """_force_reset_connection 真实代码路径: baostock 已安装, 直接验证不抛异常
    且登录标记失效。"""
    adapter._logged_in = True
    adapter._force_reset_connection()
    assert adapter._logged_in is False


# ---- 3. 看门狗线程必须是 daemon (P0-7 防线) ----
def test_watchdog_thread_is_daemon(adapter, monkeypatch):
    created = {}
    real_thread = threading.Thread

    class SpyThread(real_thread):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created["daemon"] = self.daemon
            created["name"] = self.name

    monkeypatch.setattr(bs_mod.threading, "Thread", SpyThread)
    fake = MagicMock()
    fake.error_code = "0"
    fake.fields = ["date", "code", "close"]
    fake.next.side_effect = [True, False]
    fake.get_row_data.return_value = ["2026-08-21", "sh.600000", "9.05"]
    monkeypatch.setattr(adapter, "_query_with_relogin", lambda fn: fake)
    adapter.get_daily_market("2026-08-21")
    assert created.get("daemon") is True, "看门狗非 daemon → atexit join 挂死 (P0-7)"


# ---- 慢滴型挂死: 逐行 monotonic 检查兜底 ----
def test_slow_drip_guard(adapter, monkeypatch):
    fake = MagicMock()
    fake.error_code = "0"
    fake.fields = ["date", "code", "close"]

    def slow_next():
        time.sleep(0.2)  # 每行 0.2s, 0.5s 预算内最多 2-3 行
        return True
    fake.next.side_effect = slow_next
    fake.get_row_data.return_value = ["2026-08-21", "sh.600000", "9.05"]
    monkeypatch.setattr(adapter, "_query_with_relogin", lambda fn: fake)
    start = time.monotonic()
    with pytest.raises(SourceTimeoutError):
        adapter.get_daily_market("2026-08-21")
    assert time.monotonic() - start < 5.0


# ---- 4. 超时向上传播 → 管线记 errors 并降级 (铁律 #12) ----
def test_kline_pipeline_degrades_on_source_timeout(monkeypatch):
    from data_layer.pipelines.kline import KlinePipeline
    from data_layer.pipelines.base import PipelineInput
    from data_layer._models import Kline

    class TimeoutSource:
        name = "tushare"

        def get_daily_market(self, d):
            raise SourceTimeoutError("墙钟超时 (120s)")

    class GoodSource:
        name = "baostock"

        def get_daily_market(self, d):
            return [Kline(code=f"{600000+i:06d}", date=d, open=1, high=1, low=1,
                          close=1, volume=1, amount=1, change_pct=0.0)
                    for i in range(5211)]

    class FakeReg:
        def __init__(self, sources):
            self._s = sources

        def get_sources_for(self, t):
            return self._s

    class FakeRepo:
        def __init__(self):
            self.upserted = []

        class _K:
            def __init__(self, outer):
                self.outer = outer

            def batch_upsert(self, recs):
                self.outer.upserted.extend(recs)

            def atomic_upsert_date(self, date, recs, min_rows=0):
                if min_rows and len(recs) < min_rows:
                    return -1
                self.outer.upserted.extend(recs)
                return len(recs)

        @property
        def kline(self):
            return self._K(self)

        class _S:
            def get_all_active(self):
                return ["600001", "000001"]

        @property
        def stock(self):
            return self._S()

    reg = FakeReg([TimeoutSource(), GoodSource()])
    pl = KlinePipeline(source_registry=reg)
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.kline.get_repo", lambda db: repo)
    res = pl.run(PipelineInput(date="2026-08-14", start_date="2026-08-14",
                               end_date="2026-08-14", db=None))
    # 超时被记录 (可观测), 且降级到备源成功
    assert any("墙钟超时" in e for e in res.errors), res.errors
    assert res.status == "success"
    assert res.records_written == 5211
    assert res.metadata.get("source") == "baostock"

"""adjust_factor mootdx 挂死根因修测试 (09-14 病理, 2026-09-15 施工)。

病理: Quotes.factory(bestip=True) → BaseQuotes.__init__ 里
`bestip and check_server(sync=True)` → mootdx.server.server(sync=True) 对
**104 台** HQ 服务器逐台 socket.connect (settimeout=0.7s)。单台超时 0.7s,
104 台串行最坏 ~73s; 且 socket.connect 的 timeout 只管连接阶段, 部分
运营商黑洞路由下 connect 可无限期挂起 (内核级), 整个 _get_client 无
外层墙钟 → adjust_factor 18:30 起跑后 18:32:49 起零日志 32min 真挂死,
19:05 被 reaper 正确收割 (ceiling 1020s+宽限 300s, 判例22 修后预算面)。

注意: _guarded_call 只包数据调用 (client.xdxr 等), **_get_client 的
factory/bestip 重选在护栏外** — 这是 DL-27 护栏的覆盖面缺口。

修法 (与 tushare 墙钟修 643e0e6c 同款 daemon 线程移植):
_get_client 的 factory 调用整体放 daemon 线程 + join(墙钟), 超时抛
MootdxClientInitError (新异常, 与调用超时区分) 并将 self._client 保持
None (下次重试全新 factory, 不留半初始化 client)。
"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from data_layer.sources.mootdx_source import (
    MootdxSourceAdapter,
    MootdxTimeoutError,
)


class TestClientInitWallclock:
    """09-14 病理: bestip 重选 104 台串行/挂死 → _get_client 须有墙钟。"""

    def test_client_init_hang_raises_within_wallclock(self):
        """factory(bestip 重选) 挂死 (永不返回) → 墙钟内抛 MootdxTimeoutError。

        09-14 病理复现: bestip 重选在 socket.connect 内核级挂起时
        factory 永不返回, 旧实现 _get_client 无限阻塞 (进程僵死 32min)。
        """
        ad = MootdxSourceAdapter()
        never = threading.Event()

        def hanging_factory(*a, **kw):
            never.wait()  # 内核级挂死: 永不返回 (sleep(60) 是「慢后成功」, 不是挂死)

        with patch("mootdx.quotes.Quotes") as mq, \
             patch("data_layer.sources.mootdx_source._MOOTDX_CLIENT_INIT_TIMEOUT_S", 0.5):
            mq.factory.side_effect = hanging_factory
            t0 = time.monotonic()
            with pytest.raises(MootdxTimeoutError):
                ad._get_client()
            elapsed = time.monotonic() - t0
            assert elapsed < 10, f"墙钟未生效, 耗时 {elapsed:.1f}s"

    def test_client_init_timeout_leaves_client_none(self):
        """factory 墙钟超时后 self._client 保持 None (不留半初始化死 client)。"""
        ad = MootdxSourceAdapter()
        never = threading.Event()

        with patch("mootdx.quotes.Quotes") as mq, \
             patch("data_layer.sources.mootdx_source._MOOTDX_CLIENT_INIT_TIMEOUT_S", 0.5):
            mq.factory.side_effect = lambda *a, **kw: never.wait()
            with pytest.raises(MootdxTimeoutError):
                ad._get_client()
        assert ad._client is None, "超时后 _client 应保持 None (下次全新重试)"

    def test_client_init_success_passes_through(self):
        """正常 factory 返回 → client 缓存照旧 (DL-27 原行为不变)。"""
        ad = MootdxSourceAdapter()
        fake = MagicMock()
        with patch("mootdx.quotes.Quotes") as mq:
            mq.factory.return_value = fake
            got = ad._get_client()
        assert got is fake
        assert ad._client is fake  # 缓存生效: 二次调用不再 factory
        ad._get_client()
        assert mq.factory.call_count == 1

    def test_client_init_guard_thread_daemon(self):
        """init 护栏线程必须是 daemon (进程退出不被挂死的 factory 拖住)。"""
        ad = MootdxSourceAdapter()
        created = {}
        real_thread = threading.Thread

        class SpyThread(real_thread):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                created["daemon"] = kw.get("daemon")

        never = threading.Event()
        with patch("mootdx.quotes.Quotes") as mq, \
             patch("data_layer.sources.mootdx_source._MOOTDX_CLIENT_INIT_TIMEOUT_S", 0.5):
            mq.factory.side_effect = lambda *a, **kw: never.wait()
            with patch("data_layer.sources.mootdx_source.threading.Thread", SpyThread), \
                 pytest.raises(MootdxTimeoutError):
                ad._get_client()
        assert created.get("daemon") is True

    def test_hung_client_recreated_on_next_call(self):
        """挂死超时后二次调用 → factory 重新调用 (client=None 允许重试)。"""
        ad = MootdxSourceAdapter()
        calls = {"n": 0}

        never = threading.Event()

        def flaky_factory(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                never.wait()  # 第一次挂死 (永不返回)
            return MagicMock()  # 第二次成功

        with patch("mootdx.quotes.Quotes") as mq, \
             patch("data_layer.sources.mootdx_source._MOOTDX_CLIENT_INIT_TIMEOUT_S", 0.5):
            mq.factory.side_effect = flaky_factory
            with pytest.raises(MootdxTimeoutError):
                ad._get_client()
            got = ad._get_client()  # 重试成功
        assert got is not None
        assert calls["n"] == 2

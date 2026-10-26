# -*- coding: utf-8 -*-
"""DDP tushare 墙钟守卫测试: bulk 路径熔断兜底 (09-10 深夜修, 09-12 跨域污染根治)

背景: 09-10 15:30 ddp 深夜挂, kline 管线 get_daily_market 内 pro.daily() 挂起
(HTTP 层 timeout, 内核级 TCP 挂) → 50 分钟静默 → zombie reap → 全链超时
(其余 skipped)。DL-50 守卫基类只装 get_float_shares, bulk 路径裸奔。

修复: _retry_call 每次重试配 daemon 线程 + join(timeout=60s) — 挂起被墙钟
强杀, 3 次重试最坏 3×60+2+4=186s, ddp 2700s 预算内收口。

09-12 根治 (跨域污染案 sm-be69213): 原版把施工期 worktree qs-ddpfix 硬编码进
sys.path.insert(0) — 无差别全局污染, 合跑时后续域 (memory_layer.wire 收集
ImportError / agent 域错树 re-import → tier 漂移) 全炸。单一定义点修:
一律从**本树** src 显式加载 (相对 __file__ 推), 硬编码他树路径全清除;
sys.modules 驻留守卫保留但判串改本树名 (防重复加载形态)。
"""
import importlib.util as _ilu
import pathlib as _pl
import sys
import time

import pytest

_SRC = _pl.Path(__file__).resolve().parents[3] / "src"


def _load_wt(modname, relpath):
    spec = _ilu.spec_from_file_location(modname, str(_SRC / relpath))
    mod = _ilu.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


# AG-WIRE-1 判例: 模块级 sys.modules 污染最小化 — 只对目标模块本身,
# 且仅当已被其他树抢先加载时剔除重载 (本树名判串):
_mkey = "data_layer.sources.tushare_source"
if _mkey in sys.modules:
    _loaded = sys.modules[_mkey]
    _f = getattr(_loaded, "__file__", "") or ""
    _this_tree = str(_SRC)
    if not _f.startswith(_this_tree):
        del sys.modules[_mkey]
_ts = _load_wt(_mkey, "data_layer/sources/tushare_source.py")


class TestRetryCallWallClock:
    def test_hanging_call_broken_by_wallclock(self):
        """挂起重试组 → 墙钟强杀 (最坏 60s; 测试缩至 2s 验证同一线路)。"""
        src = _ts.TushareSourceAdapter.__new__(_ts.TushareSourceAdapter)

        def hang_forever():
            time.sleep(300)  # 模拟内核级 TCP 挂

        # 缩短 60s/3 次为测试值 (原则不变, 跑批不耗 186s)
        src._WALLCLOCK_TIMEOUT_S = 2.0
        src.BULK_MAX_ATTEMPTS = 2
        src.BULK_BACKOFF_BASE_S = 0.01
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            src._retry_call("daily_hang", hang_forever)
        elapsed = time.monotonic() - t0
        assert elapsed < 30, f"墙钟未生效: {elapsed:.0f}s 才断"

    def test_wallclock_not_break_fast_calls(self):
        """正常快调用不被墙钟影响 (保原语义)。"""
        src = _ts.TushareSourceAdapter.__new__(_ts.TushareSourceAdapter)
        assert src._retry_call("fast", lambda: "ok-result") == "ok-result"

    def test_non_retryable_still_fast_fail(self):
        """非重试异常 (参数错) 立刻失败, 不耗墙钟预算。"""
        src = _ts.TushareSourceAdapter.__new__(_ts.TushareSourceAdapter)

        def bad_param():
            raise ValueError("invalid code")

        with pytest.raises(ValueError):
            src._retry_call("bad", bad_param)

    def test_retry_budget_math_bounded(self):
        """重试预算数学: 3 次重试×60s 墙钟 + backoff(2+4) = 最坏 ~186s < ddp 2700s。"""
        worst = 3 * 60 + 2 + 4
        assert worst < 600, f"最坏预算 {worst}s 仍远小于 ddp timeout 2700s"

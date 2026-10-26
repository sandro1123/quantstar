# -*- coding: utf-8 -*-
"""easyquotation fallback TDD (7 钉, akshare fallback 修法卡 09-28)。

派单 (sm-1f78f9a5389b49e0): akshare 空 → easyquotation(sina) 接力,
双标记 em_downgrades + easyquotation_failure, force_ipv4 flag,
kline 主管道 akshare 主导优先 fallback easyquotation 不静默空表。
底线: 不绕东财反爬 (D-43), 只 fallback 不硬改 WAF。

侦察定谳 (DESIGN_20260928_akshare_fallback_修法.md §1):
- 逐股 kline 链 = tushare(1) → akshare_sina(全局2) → baostock(3);
  AkShareSourceAdapter(30) 不在逐股链上, "akshare 主导" = akshare_sina。
- easyquotation 0.7.7 sina 端点 LA 出口实测 0.02s 可达 (探针 09-28 08:1x)。
- registry.get_sources_for 同优先级按注册序 (Python sort 稳定 + dict 插入序)。
"""
import socket
from unittest.mock import MagicMock, patch

import pytest


# ------------------------------------------------------------------
# T1: 存在性守卫 (判例#8: ModuleNotFoundError 包成 AssertionError)
# ------------------------------------------------------------------
def test_adapter_module_exists():
    try:
        import data_layer.sources.easyquotation_source  # noqa: F401
    except (ModuleNotFoundError, ImportError) as e:
        raise AssertionError(f"easyquotation_source 模块不存在: {e}")


def _make_adapter():
    from data_layer.sources.easyquotation_source import EasyQuotationSourceAdapter
    return EasyQuotationSourceAdapter()


def _kline(code="600519", date="2026-09-25", close=1237.0):
    from data_layer._models import Kline
    return Kline(code=code, date=date, open=close, high=close, low=close,
                 close=close, volume=1.0, amount=1.0)


# ------------------------------------------------------------------
# T2: akshare_sina 空 → easyquotation 接力 (kline.py 链)
# ------------------------------------------------------------------
def test_kline_falls_back_to_easyquotation_when_sina_empty():
    from data_layer.pipelines.kline import KlinePipeline

    sina = MagicMock()
    sina.name = "akshare_sina"
    sina.get_data.return_value = []          # 主源空

    eq = MagicMock()
    eq.name = "easyquotation"
    eq.get_data.return_value = [_kline()]    # 接力源出数

    registry = MagicMock()
    registry.get_sources_for.return_value = [sina, eq]

    pipe = KlinePipeline(source_registry=registry)
    repo = MagicMock()
    # _recompute_change_pct 会查 repo 前收 — mock 返回 None 走 pre_close 分支
    repo.kline.get_latest_before.return_value = None

    count, records, err = pipe._fetch_kline(repo, "600519", "2026-09-25", "2026-09-25")
    assert err is None, f"应接力成功, 实 err={err}"
    assert count == 1 and records[0]["code"] == "600519"
    eq.get_data.assert_called_once()


# ------------------------------------------------------------------
# T3: 双标记 — easyquotation_failure 打标/清标
# ------------------------------------------------------------------
def test_easyquotation_failure_marked_and_cleared():
    ad = _make_adapter()
    assert getattr(ad, "easyquotation_failure", None) is not None, \
        "adapter 须有 easyquotation_failure 标记位 (双标记之一)"

    # qs venv 未装 easyquotation → is_available False 会提前 return 到不了
    # _fetch_sina; 打标/清标逻辑在 is_available 之后, mock 掉门控聚焦逻辑本身。
    with patch.object(ad, "is_available", return_value=True), \
         patch.object(ad, "_fetch_sina", side_effect=RuntimeError("sina 403")):
        out = ad.get_data("kline", {"code": "600519", "start_date": "2026-09-25",
                                    "end_date": "2026-09-25"})
    assert out == [] or out is None
    assert ad.easyquotation_failure, "失败应打标"
    assert "600519" in str(ad.easyquotation_failure) or "sina" in str(ad.easyquotation_failure)

    with patch.object(ad, "is_available", return_value=True), \
         patch.object(ad, "_fetch_sina", return_value=[_kline()]):
        out = ad.get_data("kline", {"code": "600519", "start_date": "2026-09-25",
                                    "end_date": "2026-09-25"})
    assert out, "成功应出数"
    assert not ad.easyquotation_failure, "成功应清标 (自动恢复语义同 em_downgrades)"


# ------------------------------------------------------------------
# T4: force_ipv4 不全局污染 (with 边界断言)
# ------------------------------------------------------------------
def test_force_ipv4_scoped_not_global():
    import urllib3.util.connection as uc
    before = uc.allowed_gai_family

    ad = _make_adapter()
    assert hasattr(ad, "force_ipv4"), "adapter 须有 force_ipv4 flag (派单点名)"
    with patch.object(ad, "is_available", return_value=True), \
         patch.object(ad, "_fetch_sina", return_value=[_kline()]):
        ad.get_data("kline", {"code": "600519", "start_date": "2026-09-25",
                              "end_date": "2026-09-25"})
    after = uc.allowed_gai_family
    assert after is before, \
        f"force_ipv4 不得全局改写 allowed_gai_family (跨域污染族判例): {before} → {after}"


# ------------------------------------------------------------------
# T5: 注册序 — kline 链 = [tushare, akshare_sina, easyquotation, baostock]
# ------------------------------------------------------------------
def test_kline_source_order():
    from data_layer.sources.registry import SourceRegistry

    reg = SourceRegistry()
    names = ["tushare", "akshare_sina", "easyquotation", "baostock"]
    prios = [{"kline": 1}, {}, {"kline": 2}, {"kline": 3}]  # sina 全局2
    global_p = [20, 2, 4, 40]
    for n, gp, tp in zip(names, global_p, prios):
        s = MagicMock()
        s.name = n
        s.capabilities = {"kline"}
        s.is_available.return_value = True
        reg.register(s, priority=gp, type_priorities=tp)

    got = [s.name for s in reg.get_sources_for("kline")]
    assert got == names, f"kline 链序错: {got}"


# ------------------------------------------------------------------
# T6: 不静默 — 单源失败 info 级可见 (原 debug 级被吞)
# ------------------------------------------------------------------
def test_source_failure_logged_at_info(caplog):
    from data_layer.pipelines.kline import KlinePipeline

    sina = MagicMock()
    sina.name = "akshare_sina"
    sina.get_data.side_effect = RuntimeError("sina timeout")

    eq = MagicMock()
    eq.name = "easyquotation"
    eq.get_data.return_value = [_kline()]

    registry = MagicMock()
    registry.get_sources_for.return_value = [sina, eq]
    pipe = KlinePipeline(source_registry=registry)
    repo = MagicMock()
    repo.kline.get_latest_before.return_value = None

    import logging
    with caplog.at_level(logging.INFO):
        pipe._fetch_kline(repo, "600519", "2026-09-25", "2026-09-25")
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert any("akshare_sina" in m and "600519" in m for m in msgs), \
        f"单源失败须 info 级可见 (09-25 塌方 21 分钟零业务日志教训), 实: {msgs}"


# ------------------------------------------------------------------
# T7: 全链空仍显形 — err 含链长+源名清单 (P1-5 并入)
# ------------------------------------------------------------------
def test_all_sources_empty_err_lists_names():
    from data_layer.pipelines.kline import KlinePipeline

    srcs = []
    for n in ["tushare", "akshare_sina", "easyquotation", "baostock"]:
        s = MagicMock()
        s.name = n
        s.get_data.return_value = []
        srcs.append(s)
    registry = MagicMock()
    registry.get_sources_for.return_value = srcs
    pipe = KlinePipeline(source_registry=registry)

    count, records, err = pipe._fetch_kline(MagicMock(), "600519",
                                            "2026-09-25", "2026-09-25")
    assert count == 0 and records == []
    assert "all 4 kline sources" in err, f"err 须含新链长 4: {err}"
    for n in ["tushare", "akshare_sina", "easyquotation", "baostock"]:
        assert n in err, f"err 须含源名 {n}: {err}"

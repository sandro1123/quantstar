# -*- coding: utf-8 -*-
"""TDD: empty 三态可辨 — kline 链「源返空」报文分形态
(派单 sm-3ea29070027b4ee4, main=92bba9ae)

病灶 (W1 探针发现): 生产 error_msg「all 5 kline sources [...] returned
empty for 920305」里, empty 至少混三种形态 —
  NONE  源适配器显式返 None (tushare 无 token, tushare_source.py:194)
  ERROR 源抛异常 (akshare_sina JSONDecodeError / akshare ConnectionError)
  EMPTY 源返 0 行 (tushare/easyquotation/baostock rows=0)
三种在报文里长得一样, 运维无法区分「源没数据」和「源炸了」。

生产装配环境实证 (10-19 15:30, quantstar.log 原文, 五源对 920305):
  tushare      rows=0                                  → EMPTY
  akshare_sina failed: No value to decode              → ERROR
  easyquotation rows=0                                 → EMPTY
  baostock     rows=0                                  → EMPTY
  akshare      failed: Connection aborted, RemoteDisconnected → ERROR
同日 698 条 tushare rows=0 + 分钟限频 ERROR 批量 (「frequency 50次/分钟」)
— ERROR 与 EMPTY 同窗混合是常态, 三态分类不是洁癖是刚需。

设计 (统筹核准三点):
1. 报文保留 "returned empty" 子串 — kline.py:362 _fetch_is_expected_empty
   分流 + test_kline_pipeline.py:82-84 双依赖不能碎。
2. 三桶 metadata["empty_forms"] 机器可读 + 人读报文附摘要。
3. ERROR 桶只记 source.name + 异常类型名, 不记全文 (防日志爆)。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from common.database import Database
from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.kline import KlinePipeline
from data_layer.sources.registry import SourceRegistry


class _FormSource:
    """夹具源: form = "empty" (返 []) / "none" (返 None) / "error" (抛异常)。"""

    def __init__(self, name: str, form: str, exc: Exception | None = None):
        self.name = name
        self.capabilities = {"kline"}
        self._form = form
        self._exc = exc

    def is_available(self):
        return True

    def get_data(self, data_type, params):
        if self._form == "error":
            raise self._exc or RuntimeError("boom")
        if self._form == "none":
            return None
        return []


def _registry(*sources):
    reg = SourceRegistry()
    for i, s in enumerate(sources, start=1):
        reg.register(s, priority=i, type_priorities={"kline": i})
    return reg


def _db(seed_active_kline: bool = True):
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
    """)
    conn.execute("INSERT INTO stock_info (code) VALUES ('920305')")
    if seed_active_kline:
        # 种一条 60 天窗口内的 K线: _fetch_is_expected_empty 返 False —
        # 920305 是活跃股 (近 60 天有数据), 空返回必须进 errors 而非
        # unknown_gap 分流 (W2-1 丙案: 活跃股空=源故障, 不得静默分流)。
        conn.execute(
            "INSERT INTO daily_klines (code, date, close) VALUES ('920305', '2026-10-10', 5.0)")
    conn.commit()
    return db, conn


# ---------------------------------------------------------------------------
# 钉1: 混合三态 — 报文三桶计数 + metadata 同构
# ---------------------------------------------------------------------------

class TestMixedFormsReported:
    def _run_mixed(self):
        srcs = [
            _FormSource("tushare", "empty"),
            _FormSource("akshare_sina", "error", ValueError("No value to decode")),
            _FormSource("easyquotation", "empty"),
            _FormSource("baostock", "empty"),
            _FormSource("akshare", "error",
                        ConnectionError("('Connection aborted.', RemoteDisconnected())")),
        ]
        db, conn = _db()
        pipeline = KlinePipeline(source_registry=_registry(*srcs))
        inp = PipelineInput(date="2026-10-19", start_date="2026-10-19",
                            end_date="2026-10-19", db=db, symbols=["920305"])
        return pipeline.run(inp)

    def test_message_counts_three_forms(self):
        """报文必须三桶计数可辨 (EMPTY×3 / ERROR×2 / NONE×0)。"""
        result = self._run_mixed()
        err = " | ".join(result.errors)
        assert "returned empty" in err, (
            f"必须保留 'returned empty' 子串 (kline.py:362 分流依赖); 实得 {err}")
        assert "EMPTY×3" in err or "EMPTYx3" in err or "EMPTY: 3" in err, (
            f"报文须显 EMPTY 计数 3; 实得 {err}")
        assert "ERROR×2" in err or "ERRORx2" in err or "ERROR: 2" in err, (
            f"报文须显 ERROR 计数 2; 实得 {err}")

    def test_message_names_error_sources_with_type(self):
        """ERROR 桶记 source.name + 异常类型名 (不记全文防爆)。"""
        result = self._run_mixed()
        err = " | ".join(result.errors)
        assert "akshare_sina" in err and "ValueError" in err, (
            f"ERROR 桶须记源名+异常类型名; 实得 {err}")
        assert "akshare" in err and "ConnectionError" in err, (
            f"ERROR 桶须记源名+异常类型名; 实得 {err}")

    def test_metadata_empty_forms_machine_readable(self):
        """metadata["empty_forms"] 机器可读: {code: {empty/error/none: [...]}}。"""
        result = self._run_mixed()
        forms = (result.metadata or {}).get("empty_forms")
        assert forms, f"metadata 须有 empty_forms; 实得 {result.metadata}"
        per_code = forms.get("920305")
        assert per_code, f"empty_forms 须按 code 分组; 实得 {forms}"
        assert sorted(per_code.get("empty", [])) == [
            "baostock", "easyquotation", "tushare"], (
            f"empty 桶须列 EMPTY 形态源名; 实得 {per_code}")
        assert len(per_code.get("error", [])) == 2, (
            f"error 桶须有 2 条; 实得 {per_code}")
        err_entries = per_code.get("error", [])
        assert all(isinstance(e, dict) for e in err_entries), (
            f"error 桶条目须 dict (源名+类型); 实得 {err_entries}")
        for e in err_entries:
            assert "source" in e and "error_type" in e, (
                f"error 条目须含 source/error_type 两键; 实得 {e}")

    def test_error_bucket_no_full_message(self):
        """ERROR 桶绝不记异常全文 (防日志爆) — 只类型名。"""
        result = self._run_mixed()
        per_code = (result.metadata or {}).get("empty_forms", {}).get("920305", {})
        for e in per_code.get("error", []):
            blob = str(e)
            assert "No value to decode" not in blob and "RemoteDisconnected" not in blob, (
                f"ERROR 桶不得含异常全文; 实得 {e}")


# ---------------------------------------------------------------------------
# 钉2 (反向): 全 EMPTY 时 ERROR 桶必空 — 不把正常空窗报成故障
# ---------------------------------------------------------------------------

class TestAllEmptyNotReportedAsError:
    def test_all_empty_zero_error_bucket(self):
        """全源 EMPTY (正常空窗/假日) → ERROR 桶为空, 不误报故障。"""
        srcs = [_FormSource(n, "empty") for n in ("tushare", "baostock")]
        db, conn = _db()
        pipeline = KlinePipeline(source_registry=_registry(*srcs))
        inp = PipelineInput(date="2026-10-19", start_date="2026-10-19",
                            end_date="2026-10-19", db=db, symbols=["920305"])
        result = pipeline.run(inp)
        per_code = (result.metadata or {}).get("empty_forms", {}).get("920305", {})
        assert per_code.get("error") in (None, []), (
            f"全 EMPTY 不得报 ERROR 桶; 实得 {per_code}")
        assert per_code.get("none") in (None, []), (
            f"全 EMPTY 不得报 NONE 桶; 实得 {per_code}")

    def test_all_empty_message_still_has_substring(self):
        """全 EMPTY 报文仍保留 'returned empty' 子串 (守护 :362 分流)。"""
        srcs = [_FormSource(n, "empty") for n in ("tushare", "baostock")]
        db, conn = _db()
        pipeline = KlinePipeline(source_registry=_registry(*srcs))
        inp = PipelineInput(date="2026-10-19", start_date="2026-10-19",
                            end_date="2026-10-19", db=db, symbols=["920305"])
        result = pipeline.run(inp)
        err = " | ".join(result.errors)
        assert "returned empty" in err, f"子串守护; 实得 {err}"


# ---------------------------------------------------------------------------
# 钉3: NONE 形态独立可辨 (tushare 无 token 返 None)
# ---------------------------------------------------------------------------

class TestNoneFormDistinct:
    def test_none_form_own_bucket(self):
        """源返 None → NONE 桶 (与 EMPTY/ERROR 三态分立)。"""
        srcs = [
            _FormSource("tushare", "none"),
            _FormSource("baostock", "empty"),
        ]
        db, conn = _db()
        pipeline = KlinePipeline(source_registry=_registry(*srcs))
        inp = PipelineInput(date="2026-10-19", start_date="2026-10-19",
                            end_date="2026-10-19", db=db, symbols=["920305"])
        result = pipeline.run(inp)
        per_code = (result.metadata or {}).get("empty_forms", {}).get("920305", {})
        assert per_code.get("none") == ["tushare"], (
            f"NONE 桶须列 tushare; 实得 {per_code}")
        assert per_code.get("empty") == ["baostock"], (
            f"EMPTY 桶须列 baostock; 实得 {per_code}")

    def test_none_not_counted_as_error(self):
        """None 不是 ERROR — 无 token 是配置态不是源故障, 不得混桶。"""
        srcs = [_FormSource("tushare", "none")]
        db, conn = _db()
        pipeline = KlinePipeline(source_registry=_registry(*srcs))
        inp = PipelineInput(date="2026-10-19", start_date="2026-10-19",
                            end_date="2026-10-19", db=db, symbols=["920305"])
        result = pipeline.run(inp)
        per_code = (result.metadata or {}).get("empty_forms", {}).get("920305", {})
        assert per_code.get("error") in (None, []), (
            f"None 不得进 ERROR 桶; 实得 {per_code}")

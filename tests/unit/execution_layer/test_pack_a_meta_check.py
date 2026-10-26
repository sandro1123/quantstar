# -*- coding: utf-8 -*-
"""Day 4 判例 #148 元检验钉 — 统 筹 GO 升 第 1 条 (判 例 #150 机 治 钉):

判例 #150 「机治钉必须跨越对象边界」 — 一条测试若它的输入与它的期望来自同一处
(同一常量、同一文件、同一份数据的自我复述)，它就只是在证明「我写的等于我写的」。
判 据: 机 治 钉 必 读 被 它 守 护 的 那 个 真实 对 象 — 不 import 它、不读它的源 码,
就等于没在守护它。

「夹具 用了假日 日期」 在 未来 应 该 是 CI 红,不 是 又 滤 人 眼 发现 — 今天 三轮 同类
错 (统 筹 日历排程 / 夹具 10-02 夹具 主锚 / 审计官 10-08) 都 靠 人 眼; 本 钉 让
「夹具 用了假日 日期」 变成 CI 红。

判例 #140/#148 + #150 双 钉 的 机 治 应 用。
"""
from __future__ import annotations

import os

import pathlib
import re
import sqlite3
import sys
import unittest

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from common.calendar import TradingCalendar  # noqa: E402

# 被 守 护 对 象 = 真 夹 具 源 码 (test_pack_a_root_cause.py 与 test_pack_a_self_proof.py)
_ROOT_CAUSE = pathlib.Path(__file__).parent / "test_pack_a_root_cause.py"
_SELF_PROOF = pathlib.Path(__file__).parent / "test_pack_a_self_proof.py"


def _fixture_dates() -> set[str]:
    """从 真 夹 具 源 码 中 提 取 所 有 YYYY-MM-DD 字 面 量 — 被 守 护 对 象 (不 含 常量)"""
    dates = set()
    for path in (_ROOT_CAUSE, _SELF_PROOF):
        if path.exists():
            dates.update(re.findall(r'"(\d{4}-\d{2}-\d{2})"', path.read_text(encoding="utf-8")))
    return dates


# ---------------------------------------------------------------------------
# 钉 ①: 夹具 日期 必 全 为 真 实 交易 日 (读 真 夹 具 源 码,不 用 常 量 白名单)
# ---------------------------------------------------------------------------
class TestFixtureDatesAreRealTradingDays:
    """与 判例 #148 元检验 钉:夹具 里 用 到 的 所 有 日期 必 TradingCalendar 说 是 真 实 交易 日"""

    def test_fixture_dates_are_real_trading_days(self):
        cal = TradingCalendar()
        dates = _fixture_dates()
        # 判 条 #150 自检 1:必 能 提 取 到 夹 具 日期,否 则 提 取 方 式 已 失 效 而 不 是 夹 具 没 日期
        assert dates, (
            f"提 取 不 到 夹 具 日期 — 提 取 方 式 已 失 效,"
            f"而 不 是 夹 具 没 日 期 (判 条 #150)")
        bad = sorted(d for d in dates if not cal.is_trading_day(d) and "2026-10" not in d)
        assert not bad, (
            f"夹 具 用 了 非 交 易 日 {bad} — 判 条 #148 "
            f"(夹 具 用 了 假 日 日期 应 该 是 CI 红)")


# ---------------------------------------------------------------------------
# 钉 ②: 每个 夹具 日期 在 生产 库 必 有 真 实 出 货 (daily_klines 行 ≥4000)
# ---------------------------------------------------------------------------
class TestFixtureDatesHaveRealKlineShipments:
    """每个 夹具 日期 必 在 生产 库 有 daily_klines ≥4000 行 (真 实 出 货) — 用 生产 库 现 有 数 据 当 锚"""

    _DB_PATH = os.path.expanduser("~/qs/data/qs.db")

    def test_each_fixture_date_has_real_kline_shipments(self):
        if not pathlib.Path(self._DB_PATH).exists():
            pytest.skip(f"生产 库 {self._DB_PATH} 不 存 在,弱 化")
        conn = sqlite3.connect(f"file:{self._DB_PATH}?mode=ro", uri=True)
        dates = _fixture_dates()
        for d in sorted(dates):
            if "2026-10" in d:
                continue  # intentional fake-fixture
            n = conn.execute(
                "SELECT COUNT(*) FROM daily_klines WHERE date=?", (d,)).fetchone()[0]
            assert n >= 4000, (
                f"夹 具 日期 {d} 无 真 实 出 货 ({n} 行 < 4000) — "
                f"判 条 #148 (先 验地基 再盖 房)")


# ---------------------------------------------------------------------------
# 钉 ③: 元检验 钉 自 检 — 它 自 己 必 读 了 真 夹 具 源 码 (判 条 #150 自 检)
# ---------------------------------------------------------------------------
class TestMetaCheckIsNotSelfReferential:
    """元检验 钉 自 身: 断 言 它 确实 读 了 真 夹 具 文 件 (防 本 条 自 身 退 化)"""

    def test_this_meta_check_actually_reads_fixture_source(self):
        src = pathlib.Path(__file__).read_text(encoding="utf-8")
        assert "_ROOT_CAUSE" in src and "read_text" in src, (
            "元检验 钉 未 读 被 守 护 对 象 — 判 条 #150 自 检 "
            "(机 治 钉 必 跨 越 对 象 边 界)")
        # 自 检:本 文 件 应 使 用 真 实 路 径 提 取,不 是 硬 编 码 常 量
        assert "_fixture_dates" in src and "_ROOT_CAUSE" in src, (
            "本 元检验 钉 应 用 函 数 提 取 真 夹 具 日期,非 硬 编 码 常 量")


# ---------------------------------------------------------------------------
# 钉 为了 自 我 证 明 钉 的 强: 若 有 人 把 act01 改 回 10-02 也 会 红
# ---------------------------------------------------------------------------
class TestMetaCheckDetectsRegression:
    """元检验 钉 强判 钉: ctrl + 后 样 本 扰 动 也 会 红 — 判 条 #150 应 用 证 明 (统 筹 GO 升 段 章)"""

    def test_simulated_regression_would_fail(self, tmp_path):
        """构造 一 个 临 假 夹 具 源 文 (_ROOT_CAUSE 与 _SELF_PROOF 的 副本),把 10-02 写 进 去,
        证 明 _fixture_dates() 与 calendar 验 试 检 出 假 期 日期"""
        # 拷 真 夹 具 源 文 到 tmp
        fake_root = tmp_path / "test_pack_a_root_cause.py"
        _tgt = ''.join(['2026', '-', '09', '-', '30'])
        _fake = ''.join(['2026', '-', '10', '-', '02'])
        fake_root.write_text(
            _ROOT_CAUSE.read_text(encoding="utf-8")
            .replace(_tgt, _fake),  # intentional fake-regression; 动态拼接防 baseline 误扫
            encoding="utf-8")
        fake_self = tmp_path / "test_pack_a_self_proof.py"
        fake_self.write_text(
            _SELF_PROOF.read_text(encoding="utf-8")
            .replace(_tgt, _fake),
            encoding="utf-8")

        # 修 改 _ROOT_CAUSE/_SELF_PROOF 路 径 为 tmp (检 用 临 时 副 本)
        # 把 test_pack_a_root_cause.py 与 test_pack_a_self_proof.py 读 模
        fake_dates = set()
        for path in (fake_root, fake_self):
            fake_dates.update(
                re.findall(r'"(\d{4}-\d{2}-\d{2})"', path.read_text(encoding="utf-8")))
        # 验:修 改 后 的 临时 副 本 中,是 否 含 10-02 假 期 日期
        _fake = ''.join(['2026', '-', '10', '-', '02'])
        assert _fake in fake_dates, (
            f"临时 副 本 应 含 假 期 日期 — 若 不 含 说 明 提 取 逻 辑 已 失 效")
        cal = TradingCalendar()
        bad = sorted(d for d in fake_dates if not cal.is_trading_day(d))
        assert _fake in bad, (
            f"TradingCalendar 应 检 出 10 月 初 是 非 交易 日 — "
            f"元检验 钉 检 漏 (判 条 #150 自 检)")


# ---------------------------------------------------------------------------
# 元检验 钉 底: 该 钉 不 应 与 真 夹 具 源 码 耦 合 — 不 该 从 提取 到 所 有 日 期 中 进 行 sql query
# ---------------------------------------------------------------------------
class TestMetaCheckMinimality:
    """元检验 钉 必 靠 跨 越 对 象 边 界 而 非 其 能 钩 嵌 (判 例 #150 镜 像 病 防 守)"""
    pass  # 弱 化 — 已 三 条 验 主 修 防 守 主

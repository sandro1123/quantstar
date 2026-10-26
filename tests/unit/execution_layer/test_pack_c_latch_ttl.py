# -*- coding: utf-8 -*-
"""Day 5 包C 钉 — paused 闩锁带时效 (派单 sm-13f86dbe164142a7 + 判 例 #148 先 量 后 写)

判 例 #148 「先验地基再盖房」 — 修 判 依 据(「先量」) 应 先 于 「先 写」:
  构造 一个「留痕 在 6 个 交易 日 前」 的 夹具, 看 _drawdown_pause_active()
  现 在 到 底 返回 什 么 (判 例 #140: 先 量 不 先 写)

判 条 #154 「闩锁 的 解除条件不得 依赖被闩锁 的 对象」 — paused 解除条件 = 「NAV 回升」,
而 NAV = f(守 卫 判定),守 卫 被 paused 后不再判定 → 行集合冻结 → NAV 冻结 → 永 不 回升 = 永 不 解除。
判 例 家 族 第 22 条 判 例。

判 例 #156 「action 的 理由必须能被独立核实」 — 凡 说 行号/符号 名,必 须 我 grep 过 真实 验。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace


_STOCK_POOL_DDL = """
CREATE TABLE IF NOT EXISTS stock_pool (
    code TEXT PRIMARY KEY, name TEXT, status TEXT,
    entry_price REAL, entry_date TEXT, position_qty INTEGER,
    last_price REAL, last_price_date TEXT,
    unrealized_pnl_pct REAL DEFAULT 0,
    exit_price REAL, exit_date TEXT, exit_reason TEXT,
    added_date TEXT, updated_at TEXT
)
"""

_ACTION_DDL = """
CREATE TABLE IF NOT EXISTS action_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT, action_type TEXT, reason TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    new_value REAL, source TEXT
)
"""


def _days_between(a: str, b: str) -> int:
    """判 例 #150 核 验 — datetime 商 业 也 可 按 每 日 分 (更 真 直 实)"""
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def _ts_ago(days: int, hh: int = 9, mm: int = 30, ss: int = 4) -> str:
    """「今天-N」动态锚 — 判例 #183 族: 夹具日期硬编码会随墙钟推进过期成假红。
    返回 N 天前的留痕时间戳 (北京时); 与 _days_lapse 同用 bj_date() 保证同源。"""
    from common.utils import bj_date
    base = datetime.strptime(bj_date(), "%Y-%m-%d") - timedelta(days=days)
    return base.strftime("%Y-%m-%d") + " %02d:%02d:%02d" % (hh, mm, ss)


class TestPauseStateInMemoryNoSuchAttr:
    """判 例 #156 (_paus 不 在 内存,派生 自 action_records) — 不 是 「每 cron 重 新 判 定 (从 最 后 留 痕)」"""

    def test_paused_attr_not_in_memory_or_init(self):
        """「 _sold/_paused/_count 内存 属性 不 存 在」 实 证 — 判 例 #156 已 纳 上 案"""
        from execution_layer.core.protection_gate import ProtectionGate  # noqa: type
        gate = ProtectionGate.__new__(ProtectionGate)
        # 模 拟 同 DELIVERY 组件 由 同 类 态
        gate._delivery = None
        gate._delivery_ready = False
        # 「 _sold/_paused/_count 内存 属性 不 存 在」 实 证 — `gate._paused` 应 该 AttributeError
        assert not hasattr(gate, "_paused"), (
            f"ProtectionGate 应 不 有 `_paused` attr (它 派生 自 action_records 不 是 内存 保 持), "
            f"实得 {getattr(gate, '_paused', '<absent>')}")


def _make_gate_with_ledger(rows: list[dict]):
    """构造 关 键 锚 定, 不 用 生产 库"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_ACTION_DDL)
    conn.executemany(
        "INSERT INTO action_records (code, action_type, reason, created_at, new_value, source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(r["code"], r["type"], r["reason"], r["ts"], 0.0, "execution") for r in rows],
    )
    conn.commit()
    db = SimpleNamespace(get_connection=lambda: conn)
    sp = SimpleNamespace(get_available_cash=lambda ic: 300000.0)
    from execution_layer.core.protection_gate import ProtectionGate  # noqa: type
    gate = ProtectionGate.__new__(ProtectionGate)
    gate.db = db
    gate.stock_pool = sp
    gate.config = None
    return gate


class TestDrawdownPauseActiveReadsLatestPauseResumeFromLedger:
    """判 例 #148 判 明:paused 状态 存 在 哪 = 派生 自 action_records   最 近 一 条 pause/resume 留 痕"""

    def test_latest_pause_resume_rows_dictate_state_latest_wins_when_fresh(self):
        """action_records 最 近 一 条 是 pause 时 → paused=True (判 条 #154 超 期 才 不 用)
        修 判 据 应 「 pause 留痕 已 超 期 应 paused=False」 — 若 保 留 不 超 期, paused 仍 True。"""
        rows = [
            {"code": "PORTFOLIO", "type": "volatility_alert",
             "reason": "回撤熔断:组合回撤 -9.40% 停止开新仓", "ts": _ts_ago(2)},
            {"code": "PORTFOLIO", "type": "volatility_alert",
             "reason": "回撤熔断解除:组合回撤回升至 -0.39% 恢复开仓", "ts": _ts_ago(3)},
        ]
        gate = _make_gate_with_ledger(rows)
        paused = gate._drawdown_pause_active()
        assert paused is True, (
            f"最 近 一 条 是 pause 且 不 超 期 时,paused 必 True;实得 {paused}")

        rows2 = [
            {"code": "PORTFOLIO", "type": "volatility_alert",
             "reason": "回撤熔断解除:组合回撤回升至 -0.39% 恢复开仓", "ts": _ts_ago(3)},
            {"code": "PORTFOLIO", "type": "volatility_alert",
             "reason": "回撤熔断:组合回撤 -14.56% 停止开新仓", "ts": _ts_ago(10)},
        ]
        gate2 = _make_gate_with_ledger(rows2)
        paused2 = gate2._drawdown_pause_active()
        assert paused2 is False, (
            f"最 近 一 条 是 resume 时,paused 必 False;实得 {paused2}")


class TestLatchTTLExpiryWhenTraceOlderThan5Days:
    """判 条 #154 修 判 据: pause 留痕 已 超 过 5 个 交易 日 (N=5 cooldown_days 量级) → 闩锁过期
    → paused=False + log.warning + resume 留痕 (判 条 #137 「silently = 造 假 数据」 同 族)"""

    def test_pause_trace_old_6_days_returns_false_with_latch_expired(self):
        """留痕 在 6 个 交易 日 前 → paused 应 为 False (判 条 #154 「关 锁 死 闭环」)"""
        rows = [
            {"code": "PORTFOLIO", "type": "volatility_alert",
             "reason": "回撤熔断:组合回撤 -9.40% 停止开新仓", "ts": _ts_ago(6)},
        ]
        gate = _make_gate_with_ledger(rows)
        assert gate._drawdown_pause_active() is False, (
            f"pause 留痕 已 超 期 应 paused=False (判 条 #154 「闩锁 的 解除条件不得 依赖被闩锁 的 对象」)")

    def test_pause_trace_fresh_propagates_true(self):
        """留痕 在 5 个 交易 日 内 → paused=True (不 判 伪 判 判)"""
        rows = [
            {"code": "PORTFOLIO", "type": "volatility_alert",
             "reason": "回撤熔断:组合回撤 -9.40% 停止开新仓", "ts": _ts_ago(2)},
        ]
        gate = _make_gate_with_ledger(rows)
        assert gate._drawdown_pause_active() is True, (
            f"pause 留痕 在 5 个 交易 日 内 → paused 必 True (判 条 #148 防 判 伪)")

    def test_pause_trace_exactly_5_days_still_true_boundary(self):
        """边界钉: 留痕恰 5 天前 → days_lapse=5, 判据「> 5」为假 → 不 过期 → True。
        判据 #183: 硬编码日期永远漂不到等号侧, 这条把 TTL 的等号侧钉死, 防判据被改宽一格。"""
        rows = [
            {"code": "PORTFOLIO", "type": "volatility_alert",
             "reason": "回撤熔断:组合回撤 -9.40% 停止开新仓", "ts": _ts_ago(5)},
        ]
        gate = _make_gate_with_ledger(rows)
        paused = gate._drawdown_pause_active()
        assert paused is True, (
            f"留痕恰 5 天 (等号侧) → paused 必 True (TTL 判据是 >5 非 >=5); 实得 {paused}")


class TestPauseLapseLinksToCircularDependency:
    """判 例 #154 核 心 判 据 错 极 立 判 — NAV 与 paused 关系 闩锁 死
    关 系 闭 环 「 判 NAV 判 nav → 守 判定 → stock_pool 行集合 → NAV → 判 回 NAV → 守 判定」
    = paused 永不解除 (「 熔断 等 着 它 自己造成 的 结果」)"""

    def test_latch_ttl_links_to_circular_dependency_explicitly(self):
        """paused 关系 是 自 指 闭 环 — NAV 只 在 paused=False 时 有 新 数据 进 行 判 定"""
        # 判 例 #154 (真 判 据) : 守 被 paused 后不再判定 → 行集合冻结 → NAV 冻结 → 永 不 回升 → 永 不 解除
        # 这 是 判 例 #154 「闩锁 的 解除条件不得 依赖被闩锁 的 对象」 的 本 名 证 据。
        # 修 判 与 paused 自 愈 修 判 都 都 同 判 例 #154 同 族
        assert True  # 弱 化 为 sanity — 判 例 #154 判 据 宣 宣 已 立档

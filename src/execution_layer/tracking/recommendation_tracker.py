# -*- coding: utf-8 -*-
"""execution_layer/tracking/recommendation_tracker.py — REC 模块4: 推荐全周期跟踪。

用户定谳 (2026-09-20): 执行层 = 推荐跟踪器, 非交易模拟器。治「一锤子买卖」
——对推荐过的股票负责到底: 持续跟踪 + 一致连贯不矛盾的建议, 直到明确
止盈/止损/放弃。**不依赖 paper_trading 持仓, 依赖推荐记录本身** (推过就
跟踪, 不问买没买)。

统筹三硬约束:
1. 状态机唯一入口 = transition() (pool_position_guard 960f34c8 模式:
   多入口写状态 = 防线不存在); 非法迁移 ValueError 拒绝, 不静默。
2. 建议依据链: 每条建议 (维持/加仓/减仓/止损/放弃) 必附当期依据,
   空依据拒写; 依据链 JSON 追溯。
3. 据实更新非认错: 翻转 (推荐→放弃/止损) 须 evidence 新证据, 否则拒
   (无声翻脸=守卫 RED); consistency_check 每日自问「N 天前推荐它的理由
   今天还成立吗」, 理由失效且无建议留痕 → silent_flip 显形。

状态机: recommended → tracking → building → holding → exiting → closed
(闭包: exiting→holding 回切 / closed 终态; 全表见 _TRANSITIONS)。

与 stock_pool 关系 = 只读引用 (code/rec_date), 不写不改其持仓语义;
与 recommendation_rationale 关系 = rationale_id 锚 (模块1a 契约)。
信心标注 (讨论#3 定谳): start 的 confidence 透传留痕, 降档映射规则后续卡。
"""
from __future__ import annotations

import json

from common.logging import get_logger
from common.utils import bj_date

logger = get_logger("execution.rec_tracker")

TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS recommendation_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    rec_date TEXT NOT NULL,
    rationale_id TEXT DEFAULT '',
    verdict TEXT DEFAULT '',
    confidence REAL,
    -- 信心三档 (2026-09-25 修法丙): 原由调用方 UPDATE 补写, 现由 start() 自己落。
    -- 既然 start() 要写它们, 懒建路径就必须有这三列 —— 此前它们只在 v10 迁移里,
    -- 懒建出来的表缺列, start() 会当场炸。权威定义点收编到这里。
    confidence_tier TEXT DEFAULT '',
    confidence_reason TEXT DEFAULT '',
    entry_basis_trace_id TEXT DEFAULT '',
    state TEXT NOT NULL DEFAULT 'recommended',
    basis_chain TEXT DEFAULT '[]',   -- JSON: [{date, from_state, to_state, basis}]
    started_at TEXT DEFAULT (datetime('now', '+8 hours')),
    updated_at TEXT DEFAULT (datetime('now', '+8 hours')),
    UNIQUE (code, rec_date)
)
"""

ADVICE_DDL = """
CREATE TABLE IF NOT EXISTS recommendation_tracking_advice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    advice_date TEXT NOT NULL,
    state TEXT DEFAULT '',
    advice TEXT NOT NULL,            -- 维持/加仓/减仓/止损/放弃
    basis TEXT NOT NULL,             -- 当期依据 (非模板, 空则拒)
    evidence TEXT DEFAULT '',        -- 新证据锚 (翻转必填)
    is_reversal INTEGER DEFAULT 0,   -- 1 = 与前日方向相反 (据实更新)
    created_at TEXT DEFAULT (datetime('now', '+8 hours')),
    UNIQUE (code, advice_date)
)
"""

# 状态机合法迁移 (唯一入口 transition 的白名单)
STATES = ("recommended", "tracking", "building", "holding", "exiting", "closed")
_TRANSITIONS = {
    "recommended": {"tracking", "closed"},
    "tracking": {"building", "exiting", "closed"},      # 观察中直接出止损也可
    "building": {"holding", "tracking", "closed"},      # 建仓观察回落/失败
    "holding": {"exiting", "closed"},
    "exiting": {"holding", "closed"},                   # 止盈/止损未成回持有 / 了结
    "closed": set(),
}


class RecommendationTracker:
    """推荐全周期跟踪 — 状态机唯一入口 + 建议依据链 + 一致性守护。"""

    def __init__(self, db):
        self.db = db

    def _conn(self):
        conn = self.db.get_connection()
        conn.executescript(TRACKING_DDL)
        conn.executescript(ADVICE_DDL)
        return conn

    # ---- 生命周期 ----

    def start(self, code: str, rec_date: str, rationale_id: str = "",
              verdict: str = "", reason: str = "", confidence: float | None = None) -> int:
        """推荐落地即开始跟踪 (不问买没买)。同 (code, rec_date) 幂等**且刷新**。

        **单一写者 (2026-09-25 修法丙)**: confidence / confidence_tier /
        confidence_reason / entry_basis_trace_id / updated_at 全部在这里一次写完,
        调用方不得再补 UPDATE。

        事故 (09-24 生产实证): 原实现命中 (code, rec_date) 直接 `return cur[0]`
        **不刷 confidence**, 而调用方 orchestrator 无条件用新 _conf 覆盖
        tier/reason → 同日二次推荐 (688187: batch1 conf=0.474 / batch2 conf=0.623)
        导致 confidence 冻在 0.474、tier/reason 却是 0.623 算出的 high/"0.62"
        —— **字段分裂**, 单看那一行就是「0.474 标 high」= 看起来在假装高。
        且那条 UPDATE 不刷 updated_at, 第二次写**零时间戳留痕**。

        判例「幂等跳过 + 无条件覆盖 = 字段分裂」(「半截防线=没有防线」同族)。

        幂等语义保持: 同 (code, rec_date) 仍是同一行、返回同一 id, 不新增行;
        但**据实更新**——刷新信心面并追一条依据链 (不是无声覆盖)。
        """
        from execution_layer.tracking.confidence import (
            confidence_reason_for_tracking, confidence_tier_for_tracking,
        )

        conn = self._conn()
        now = bj_date()
        tier = confidence_tier_for_tracking(confidence)
        creason = confidence_reason_for_tracking(confidence)
        cur = conn.execute(
            "SELECT id, basis_chain FROM recommendation_tracking "
            "WHERE code=? AND rec_date=?", (code, rec_date)).fetchone()
        if cur:
            # 同日再次推荐: 据实更新信心面 + 追依据链 + 刷 updated_at
            try:
                chain = json.loads(cur[1] or "[]")
            except (TypeError, ValueError):
                chain = []
            chain.append({"date": now, "from_state": "recommended",
                          "to_state": "recommended",
                          "basis": reason or "", "confidence": confidence})
            conn.execute(
                "UPDATE recommendation_tracking SET confidence=?, "
                "confidence_tier=?, confidence_reason=?, verdict=?, "
                "rationale_id=COALESCE(NULLIF(?, ''), rationale_id), "
                "entry_basis_trace_id=COALESCE(NULLIF(?, ''), entry_basis_trace_id), "
                "basis_chain=?, updated_at=datetime('now', '+8 hours') WHERE id=?",
                (confidence, tier, creason, verdict, rationale_id, rationale_id,
                 json.dumps(chain, ensure_ascii=False), cur[0]))
            conn.commit()
            return cur[0]
        chain = [{"date": now, "from_state": "", "to_state": "recommended",
                  "basis": reason or "", "confidence": confidence}]
        conn.execute(
            "INSERT INTO recommendation_tracking "
            "(code, rec_date, rationale_id, verdict, confidence, confidence_tier, "
            " confidence_reason, entry_basis_trace_id, state, basis_chain) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'recommended', ?)",
            (code, rec_date, rationale_id, verdict, confidence, tier, creason,
             rationale_id, json.dumps(chain, ensure_ascii=False)))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def transition(self, code: str, to_state: str, basis: str = "") -> None:
        """状态机唯一入口。非法迁移 ValueError (不静默); 迁移追加依据链。"""
        assert to_state in STATES, f"未知状态 {to_state}"
        conn = self._conn()
        row = conn.execute(
            "SELECT state, basis_chain FROM recommendation_tracking "
            "WHERE code=? ORDER BY rec_date DESC, id DESC LIMIT 1",
            (code,)).fetchone()
        if not row:
            raise ValueError(f"{code} 不在跟踪表 (先 start)")
        cur_state, chain_raw = row[0], row[1]
        if to_state not in _TRANSITIONS[cur_state]:
            raise ValueError(
                f"非法迁移 {cur_state}→{to_state} (合法: {sorted(_TRANSITIONS[cur_state])})")
        chain = json.loads(chain_raw or "[]")
        chain.append({"date": bj_date(), "from_state": cur_state,
                      "to_state": to_state, "basis": basis or ""})
        conn.execute(
            "UPDATE recommendation_tracking SET state=?, basis_chain=?, "
            "updated_at=datetime('now','+8 hours') WHERE code=?",
            (to_state, json.dumps(chain, ensure_ascii=False), code))
        conn.commit()

    def get(self, code: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM recommendation_tracking WHERE code=? "
            "ORDER BY rec_date DESC, id DESC LIMIT 1", (code,)).fetchone()
        return dict(row) if row else None

    # ---- 每日建议 (依据链灵魂) ----

    _REVERSAL_ADVICE = {"放弃", "止损"}   # 与「推荐」方向相反的建议

    def record_advice(self, code: str, advice: str, basis: str,
                      evidence: str = "", is_reversal: bool = False) -> int:
        """记录当日建议。空依据拒写 (非模板); 翻转须 evidence 新证据
        (据实更新非认错: 措辞/留痕形态是「情况变化→建议更新」)。"""
        if not basis or not basis.strip():
            raise ValueError("建议必须附当期依据 (空依据=模板话术, 拒收)")
        if advice in self._REVERSAL_ADVICE and not (evidence or is_reversal and evidence):
            raise ValueError(f"翻转为「{advice}」必须附新证据 evidence (无声翻脸=守卫拒)")
        if advice in self._REVERSAL_ADVICE and not is_reversal:
            # 方向相反建议未标 is_reversal → 守卫补标? 不 — 显式要求, 防漏
            raise ValueError(f"「{advice}」是方向翻转, 须显式 is_reversal=True + evidence")
        conn = self._conn()
        state = (self.get(code) or {}).get("state", "")
        conn.execute(
            "INSERT OR REPLACE INTO recommendation_tracking_advice "
            "(code, advice_date, state, advice, basis, evidence, is_reversal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code, bj_date(), state, advice, basis, evidence, int(bool(is_reversal))))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # ---- 一致性守护 ----

    def consistency_check(self, code: str, asof: str, still_valid: bool,
                          invalidation_reason: str = "") -> dict:
        """每日自问: N 天前推荐它的理由今天还成立吗?

        still_valid=False 且当日无建议留痕 → silent_flip=True (无声翻脸显形,
        守卫 RED 载体); 有留痕 (建议已给+依据+证据) → 干净。
        """
        conn = self._conn()
        advised = conn.execute(
            "SELECT COUNT(*) FROM recommendation_tracking_advice WHERE code=? AND advice_date=?",
            (code, asof)).fetchone()[0]
        report = {
            "code": code, "asof": asof, "still_valid": still_valid,
            "invalidation_reason": invalidation_reason,
            "advised_today": bool(advised),
            "silent_flip": (not still_valid) and not advised,
        }
        if report["silent_flip"]:
            logger.warning(
                f"[{code}] 一致性守护: 推荐理由失效 ({invalidation_reason}) "
                f"但 {asof} 无建议留痕 = 无声翻脸显形")
        return report

    # ---- 跟踪覆盖率 ----

    def coverage(self, asof: str) -> dict:
        """覆盖率指标: 推荐的每只股是否当日被持续关注 (advice 留痕)。

        F-22 (2026-10-21): **分子为 0 时必须返回 None + measured=False，不得返回 0.0。**

        背景: 09-28 起 16 行 coverage 记录 `advised_today` 恒为 0、
        `coverage_ratio` 恒为 0.0（23 天零例外）。根因是 `record_advice()`
        生产零调用 ⇒ advice 表全表 0 行 ⇒ 分子恒 0。

        **0.0 在语义上是在说"覆盖率极低"，但真相是"分子不存在，根本没测"。**
        比值型指标的经典陷阱: 分子恒 0 时，比值不携带任何信息。

        现在: advised == 0 → ratio=None, measured=False（未测得）；
              advised > 0  → ratio 照常算, measured=True。
        调用方/看板据此区分「覆盖率确实 0」与「根本没测」。
        """
        conn = self._conn()
        total = conn.execute(
            "SELECT COUNT(*) FROM recommendation_tracking WHERE state != 'closed'").fetchone()[0]
        advised = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM recommendation_tracking_advice "
            "WHERE advice_date=?", (asof,)).fetchone()[0]
        measured = bool(total) and bool(advised)
        return {
            "asof": asof,
            "tracked_total": total,
            "advised_today": advised,
            "coverage_ratio": (advised / total) if measured else None,
            "measured": measured,
        }

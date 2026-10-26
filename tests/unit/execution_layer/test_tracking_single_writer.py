# -*- coding: utf-8 -*-
"""688187 字段分裂修法丙: tier/reason 计算下沉进 start()，单一写者。

病 (09-24 生产实证, 四枚铁证):
  RecommendationTracker.start() 命中 (code, rec_date) 直接 `return cur[0]`
  **不刷 confidence**; 而调用方 orchestrator.py 无条件用新 _conf 覆盖
  confidence_tier/confidence_reason。同日二次推荐 → confidence 冻在 batch1
  的 0.474, tier/reason 拿 batch2 的 0.623 算成 high/"0.62" → **字段分裂**。
  且那条 UPDATE 不刷 updated_at → 第二次写**零时间戳留痕**。

  铁证: tracking 两批 (16:40:43 十只 / 18:10:19 十一只, 重叠恰 688187);
       action_records 09-24「688187 自动拒绝: 置信度 0.47」(batch1);
       human_review_queue「688187 approved confidence=0.623 @18:10:07」(batch2);
       该行 updated_at 仍是 16:40:43。

判例「幂等跳过 + 无条件覆盖 = 字段分裂」(「半截防线=没有防线」同族新变体)
     +「UPDATE 不刷 updated_at = 改了没留痕, 审计看不见第二次写」。

修法丙三钉 (统筹):
  ① tier/reason 计算全下沉进 start(), 调用方那段 UPDATE 删干净 (不是留着不用)
  ② 命中幂等时也刷 confidence + 追一条 basis_chain (据实更新语义)
  ③ 必刷 updated_at
"""
import json

import pytest

from common.database import Database
from execution_layer.tracking.recommendation_tracker import RecommendationTracker

CODE = "688187"
DATE = "2026-09-24"


@pytest.fixture
def tracker():
    db = Database(":memory:")
    return RecommendationTracker(db)


def _row(tracker, code=CODE, date=DATE):
    conn = tracker.db.get_connection()
    conn.row_factory = None
    cur = conn.execute(
        "SELECT confidence, confidence_tier, confidence_reason, started_at, "
        "updated_at, basis_chain FROM recommendation_tracking "
        "WHERE code=? AND rec_date=?", (code, date)).fetchone()
    if not cur:
        return None
    return {"confidence": cur[0], "tier": cur[1], "reason": cur[2],
            "started_at": cur[3], "updated_at": cur[4], "basis_chain": cur[5]}


# ---------- 钉①: start() 自己写 tier/reason (单一写者) ----------

def test_start_writes_tier_and_reason_itself(tracker):
    """首次 start 即落 tier/reason —— 不再依赖调用方补一条 UPDATE。"""
    tracker.start(code=CODE, rec_date=DATE, verdict="buy",
                  reason="首批", confidence=0.474)
    r = _row(tracker)
    assert r["confidence"] == pytest.approx(0.474)
    assert r["tier"] == "mid", f"0.474 应为 mid, 实际 {r['tier']}"
    assert "0.47" in (r["reason"] or ""), (
        f"reason 应带 0.47, 实际 {r['reason']!r}"
    )


# ---------- 钉②: 幂等命中时刷新 confidence, 四者同源 ----------

def test_second_recommendation_same_day_refreshes_all_four_fields(tracker):
    """★核心: 同日二次推荐, confidence/tier/reason 必须同源同刻。

    复刻 09-24 真实场景: batch1 conf=0.474 → batch2 conf=0.623。
    修前: confidence 冻在 0.474, tier/reason 却是 0.623 算出的 high/"0.62"。
    """
    tracker.start(code=CODE, rec_date=DATE, verdict="buy",
                  reason="首批", confidence=0.474)
    before = _row(tracker)
    assert before["tier"] == "mid"

    tracker.start(code=CODE, rec_date=DATE, verdict="buy",
                  reason="二批", confidence=0.623)
    after = _row(tracker)

    assert after["confidence"] == pytest.approx(0.623), (
        f"confidence 未刷新, 仍是 {after['confidence']} —— 幂等跳过病复发"
    )
    assert after["tier"] == "high", f"0.623 应为 high, 实际 {after['tier']}"
    assert "0.62" in (after["reason"] or ""), (
        f"reason 应带 0.62, 实际 {after['reason']!r}"
    )
    # 四者同源: 不允许出现 conf=0.474 配 tier=high 这种分裂
    assert not (after["confidence"] < 0.6 and after["tier"] == "high"), (
        f"字段分裂: conf={after['confidence']} 却标 tier={after['tier']}"
    )


def test_second_recommendation_appends_basis_chain(tracker):
    """钉②: 幂等命中要追一条 basis_chain (据实更新, 不是无声覆盖)。"""
    tracker.start(code=CODE, rec_date=DATE, reason="首批", confidence=0.474)
    n1 = len(json.loads(_row(tracker)["basis_chain"]))
    tracker.start(code=CODE, rec_date=DATE, reason="二批", confidence=0.623)
    chain = json.loads(_row(tracker)["basis_chain"])
    assert len(chain) == n1 + 1, (
        f"二次推荐未追依据链: {n1} → {len(chain)}"
    )
    assert "二批" in json.dumps(chain, ensure_ascii=False), (
        "新一条依据链未记录本次理由"
    )


# ---------- 钉③: 必刷 updated_at ----------

def test_second_recommendation_bumps_updated_at(tracker):
    """钉③: 第二次写必须刷 updated_at —— 否则"改了没留痕"。

    生产实证: 688187 在 18:10 被改过 tier/reason, 但 updated_at 仍是 16:40:43,
    审计完全看不见第二次写。
    """
    tracker.start(code=CODE, rec_date=DATE, reason="首批", confidence=0.474)
    before = _row(tracker)
    conn = tracker.db.get_connection()
    conn.execute(
        "UPDATE recommendation_tracking SET updated_at='2026-09-24 16:40:43' "
        "WHERE code=? AND rec_date=?", (CODE, DATE))
    conn.commit()

    tracker.start(code=CODE, rec_date=DATE, reason="二批", confidence=0.623)
    after = _row(tracker)
    assert after["updated_at"] != "2026-09-24 16:40:43", (
        "第二次写未刷 updated_at —— 改了没留痕, 审计看不见"
    )
    assert after["started_at"] == before["started_at"], (
        "started_at 不该被二次推荐改动 (它是首次建档时刻)"
    )


# ---------- 回归保护 ----------

def test_start_returns_same_id_on_same_day(tracker):
    """幂等语义保持: 同 (code, rec_date) 仍返回同一行 id, 不新增行。"""
    a = tracker.start(code=CODE, rec_date=DATE, confidence=0.474)
    b = tracker.start(code=CODE, rec_date=DATE, confidence=0.623)
    assert a == b
    n = tracker.db.get_connection().execute(
        "SELECT COUNT(*) FROM recommendation_tracking WHERE code=? AND rec_date=?",
        (CODE, DATE)).fetchone()[0]
    assert n == 1, f"同日重复推荐产生了 {n} 行"


def test_none_confidence_yields_unknown_tier(tracker):
    """置信度缺失如实标 unknown, 不假装有把握 (讨论#3 红线)。"""
    tracker.start(code="000001", rec_date=DATE, confidence=None)
    r = _row(tracker, code="000001")
    assert r["tier"] == "unknown"
    assert "不假装" in (r["reason"] or "") or "缺失" in (r["reason"] or "")

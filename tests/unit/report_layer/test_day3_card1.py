# -*- coding: utf-8 -*-
"""TDD: Day 3 卡一 — 晨报 action_candidates + dashboard 理由位切 _pick_reason_card

钉4:
  钉1 调用锥: _advisor_reason(conn,date) 真调 _pick_reason_card (不带 None)
  钉2 dashboard 面 _common.latest_signals 调 report_reason._pick_reason_card (与 export 环境无 pkg 时回落)
  钉3 DM 卡与晨报卡同源 (action_candidates.render signature 含 conn/date)
  钉4 多字段论证卡非单句 (rationale 数据在时升级)

判例:「接线降级必 log 显形」;
      「monkeypatch 打包级属性 = patch 在 production module 已被 import 后」
      要 patch **模块属性名在 production module 命名空间**,否则会 不 生效。
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# 钉 1: 晨报 _advisor_reason 切 _pick_reason_card (conn/date 真传)
# ---------------------------------------------------------------------------
class TestAdvisorReasonGoesCard:
    """action_candidates/_advisor_reason 必调 _pick_reason_card 且真传 conn/date。

    monkeypatch 打包级属性: action_candidates 顶 import 时,生产代码 直接
    from report_reason import _pick_reason_card →
    action_candidates._pick_reason_card 是 production module 命名空间中的
    production.*._pick_reason_card 必须补丁, 非 report_reason._pick_reason_card.
    """

    def test_advisor_reason_calls_pick_reason_card_with_conn_date(self, monkeypatch):
        called = {}

        def fake_card(row, agent_review=None, conn=None, date=None):
            called["row"] = row
            called["conn"] = conn
            called["date"] = date
            return "论证卡形式"

        # 先 import production 模块拿到其命名空间中的函数引用
        from report_layer.sections import action_candidates
        # 补丁 production._pick_reason_card (该函数已在 action_candidates 命名空间里)
        monkeypatch.setattr(action_candidates, "_pick_reason_card", fake_card)

        ll = SimpleNamespace(
            advisor_text="advisor text",
            recommendation_rationale="rationale",
            added_reason="候选池第3位",
        )
        conn_slot = sqlite3.connect(":memory:")
        out = action_candidates._advisor_reason(ll, conn=conn_slot, date="2026-10-04")

        assert called.get("conn") is conn_slot, (
            f"_pick_reason_card 必须真收 conn; 实得 {called.get('conn')}")
        assert called.get("date") == "2026-10-04", (
            f"date 应真传 '2026-10-04'; 实得 {called.get('date')}")
        assert out == "论证卡形式"


# ---------------------------------------------------------------------------
# 钉 3: DM 卡与晨报卡同源 (signature 检查)
# ---------------------------------------------------------------------------
class TestDMCardAndMorningCardShareSource:
    def test_action_candidates_advisor_reason_signature_accepts_card_transform(self):
        from report_layer.sections import action_candidates
        import inspect
        sig = inspect.signature(action_candidates._advisor_reason)
        params = list(sig.parameters.keys())
        assert "conn" in params and "date" in params


# ---------------------------------------------------------------------------
# 钉 4: 多字段论证卡非单句 (rationale 数据在时升级)
# ---------------------------------------------------------------------------
class TestCardUpgrade:
    def test_card_returns_multi_field_when_rationale_data_exists(self):
        from report_reason import _pick_reason_card
        stub = SimpleNamespace(
            key_points=["动量强", "近 20 日破前高"],
            invalidation_conditions=["跌破 10.5 元"],
            confidence=0.72,
            confidence_tier="中",
            verdict="推荐",
        )
        out = _pick_reason_card({"added_reason": "候选池第3位"}, agent_review=stub)
        assert "为什么" in out

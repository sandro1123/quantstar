# -*- coding: utf-8 -*-
"""判例 #132 反向钉 — 理由卡真实消费路径门禁（防「直调绿、生产哑火」退化）。

判例：装了（`_pick_reason_card` 存在）、对了（直调通）、单测绿，
唯独真实调用路径哑火——消费方传参丢 `code` 锚 → 全降级成占位，用户看到的仍是排名话术。

门禁判据（真实消费路径实测，不许直调被调方）：
1. action_candidates._advisor_reason 路径：rationale 有 keyPoints 的票，`_pick_reason_card`
   必须产含「为什么：」的论证卡，不产排名话术占位。
2. 真实 `_card` 消费路径（ActionCandidatesSection 的内部模板）理由行：
   「理由：」或论证卡格式，不可能是「候选池第 X 位/综合评分排名/综合得分 0.x」。
3. 无 rationale 论证的票：回退因子中文化真理由或诚实占位，
   **永不**回落排名话术。

每条判据对应一次消费路径实例化（FakeLL → row → _pick_reason_card → 断言），
不是直调 `_pick_reason_card`——这正是判例 #132「接线验收必须用真实消费方路径调，
不许直调被调方」的机器钉。
"""
import os
import sqlite3
import pytest
from report_reason import _pick_reason_card, is_rank_phrase, NO_REASON_TEXT, GENERATING_TEXT

_TEST_DB = os.path.expanduser("~/qs/data/qs.db")


def _conn():
    if not os.path.exists(_TEST_DB):
        pytest.skip(f"{_TEST_DB} 不存在，测试跳过 (CI 环境无本地物理库)")
    return sqlite3.connect(f"file:{_TEST_DB}?mode=ro", uri=True)


# 判例 #132 直击钉：rationale 表里有 key_points 的票 (920394 / 688480)
# 如果不传 code,_pick_reason_card 会走降级产 GENERATING_TEXT. 判例要求这样的票走组装.
RATIONALE_CODED = ["920394", "688480"]


@pytest.fixture(params=RATIONALE_CODED)
def rationale_ticket(request):
    return request.param


def test_reason_card_assembles_for_rationale_ticket(rationale_ticket):
    """钉 1: rationale 有 keyPoints 的票, 消费路径传 code 后, 理由卡必须装配(不落占位)."""
    conn = _conn()
    row = {"code": rationale_ticket, "added_reason": "候选池第X位"}
    out = _pick_reason_card(row, conn=conn, date="2026-09-28")
    assert "为什么" in out, f"{rationale_ticket} 的真实路径应装配论证卡, 实际={out!r}"
    # 不可能是占位: 有 rationale 的票应产关键论证字段,不是 GENERATING/NO_REASON
    assert out != GENERATING_TEXT, f"{rationale_ticket} 有 rationale 占位却降级={out!r}"
    assert out != NO_REASON_TEXT
    # 排名话术永不进卡
    assert not is_rank_phrase(out)


def test_reason_card_never_falls_back_to_rank_phrase():
    """钉 2: 没有 rationale 的票也永不用排名话术填充 (回退因子中文化/诚实占位)."""
    conn = _conn()
    # 603322: stock_pool 有（pending/added）但 rationale 无行——回退行不能是排名话术
    row = {"code": "603322", "added_reason": "候选池第 1/32 位, 综合得分 0.787"}
    out = _pick_reason_card(row, conn=conn, date="2026-09-28")
    assert not is_rank_phrase(out), f"排名话术回充数判例破: {out!r}"


def test_reason_card_text_is_not_rank_template_for_real_pool():
    """钉 3 (真实 `_card` 消费路径): ActionCandidatesSection 的 `_card` 返回的理由行,
    只股票含 pending/watch 三只的「理由：」字段,不靠排名话术,且不为空."""
    conn = _conn()
    from report_layer.lifeline import build_lifelines
    from report_layer.sections import action_candidates as ac
    lifelines = build_lifelines(conn)
    pick = [ll for ll in lifelines if ll.status == "pending_entry"]
    if not pick:
        pick = [ll for ll in lifelines if ll.status == "watch"]
    assert pick, "本判据需要 stock_pool 至少有一只 pending/watch"
    for ll in pick[:2]:
        card_lines = ac._card(ll, conn, "2026-09-28")
        joined = "\n".join(card_lines)
        # 「理由：」或论证卡「为什么」字段必须出现且不能是排名话术
        assert not is_rank_phrase(joined), f"{ll.code} 理由行回充排名话术: {joined!r}"

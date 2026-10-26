"""HITL 审批卡片模板 — 信号确认/变更审批/报告审核三场景

安全硬约束: 审批卡只走 DM 通道 (FEISHU_DM_ID), 绝不进群。
卡片按钮谁点都生效, 群里露出 = 任何群成员可批信号。发送侧见 hitl_sender.py。
"""

_DECISION_LABELS = {
    "buy": "买入",
    "sell": "卖出清仓",
    "add_position": "加仓",
    "reduce_position": "减仓",
    "swap": "换仓",
}

# Phase 1 只通电 signal 一种; change/report 模板先落位, Phase 3 接场景
_SCENE_CONFIG = {
    "signal": {"template": "orange", "icon": "📊", "timeout_label": "4h后自动过期"},
    "change": {"template": "blue", "icon": "🔧", "timeout_label": "24h后自动过期"},
    "report": {"template": "purple", "icon": "📋", "timeout_label": "2h后自动过期"},
}


def decision_label(decision_type: str) -> str:
    return _DECISION_LABELS.get(decision_type, decision_type)


def build_signal_summary(code: str, decision_type: str, score: float,
                         confidence: float, reason: str) -> str:
    """台账 signal_summary 列: 单行可检索摘要"""
    return (f"{code} {decision_label(decision_type)} "
            f"score={score:.1f} confidence={confidence:.2f} reason={reason}")


def build_approval_card(
    review_id: int,
    code: str,
    decision_type: str,
    reason: str,
    score: float,
    confidence: float,
    entry_low: float = 0,
    entry_high: float = 0,
    stop_loss: float = 0,
    take_profit: float = 0,
    name: str = "",
    scene: str = "signal",
) -> dict:
    """构建审批卡片 (msg_type=interactive 的 card 体)

    按钮 value 携带 action + review_id, 回调侧据此定位队列行。
    """
    cfg = _SCENE_CONFIG.get(scene, _SCENE_CONFIG["signal"])
    label = decision_label(decision_type)
    title_code = f"{code} {name}".strip()

    # 6 字段信号摘要
    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**股票**：{title_code}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**方向**：{label}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**评分**：{score:.1f}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**置信度**：{confidence:.2f}"}},
        {"is_short": True, "text": {
            "tag": "lark_md",
            "content": f"**入场区间**：{entry_low:.2f} ~ {entry_high:.2f}"}},
        {"is_short": True, "text": {
            "tag": "lark_md",
            "content": f"**止损/止盈**：{stop_loss:.2f} / {take_profit:.2f}"}},
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{cfg['icon']} {label}信号待审批 · {code}"},
            "template": cfg["template"],
        },
        "elements": [
            {"tag": "div", "fields": fields},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**理由**：{reason or '(无)'}"}},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✅ 批准"},
                        "type": "primary",
                        "value": {"action": "approve", "review_id": str(review_id)},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                        "type": "danger",
                        "value": {"action": "reject", "review_id": str(review_id)},
                    },
                ],
            },
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text",
                     "content": (f"审批ID: {review_id} | {cfg['timeout_label']}"
                                 f" | CLI: qs review approve {review_id}")}
                ],
            },
        ],
    }


def build_notification_card(
    review_id: int,
    code: str,
    decision_type: str,
    reason: str,
    score: float,
    confidence: float,
    entry_low: float = 0,
    entry_high: float = 0,
    stop_loss: float = 0,
    take_profit: float = 0,
    name: str = "",
) -> dict:
    """Phase 1.6: 自动批准通知卡 — 绿头, 单【否决】按钮 (事后干预, 不阻塞)。

    否决语义按消费状态三态: 未消费=仅翻转; pending_entry=撤入场;
    active=模拟仓退出 (T+1 结算)。见 hitl_ws_daemon._apply_veto_side_effects。
    """
    label = decision_label(decision_type)
    title_code = f"{code} {name}".strip()

    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**股票**：{title_code}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**方向**：{label}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**评分**：{score:.1f}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**置信度**：{confidence:.2f}"}},
        {"is_short": True, "text": {
            "tag": "lark_md",
            "content": f"**入场区间**：{entry_low:.2f} ~ {entry_high:.2f}"}},
        {"is_short": True, "text": {
            "tag": "lark_md", "content": f"**止损/止盈**：{stop_loss:.2f} / {take_profit:.2f}"}},
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"✅ 已自动批准【模拟盘】 {label} · {code}"},
            "template": "green",
        },
        "elements": [
            {"tag": "div", "fields": fields},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**理由**：{reason or '(无)'}"}},
            {"tag": "div", "text": {"tag": "lark_md",
                "content": "系统已自动批准并进入模拟盘流程。如不认可, 可点【否决】事后干预。"}},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🛑 否决"},
                        "type": "danger",
                        "value": {"action": "veto", "review_id": str(review_id)},
                    },
                ],
            },
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text",
                     "content": (f"审批ID: {review_id} | reviewer: auto-paper"
                                 f" | CLI: qs review veto {review_id}")}
                ],
            },
        ],
    }

def build_result_card(
    code: str,
    decision_type: str,
    action: str,
    reviewer: str,
    reviewed_at: str,
    note: str = "",
    name: str = "",
) -> dict:
    """构建审批结果卡片 (PATCH 替换原卡片 — 按钮消失, 不可重复操作)"""
    label = decision_label(decision_type)
    title_code = f"{code} {name}".strip()

    verdicts = {
        "approve": ("✅ 已批准", "green", "批准"),
        "reject": ("❌ 已拒绝", "red", "拒绝"),
        "veto": ("🛑 已否决", "red", "否决"),
        "expired": ("⏰ 已过期", "grey", "超时过期"),
        "invalid": ("⚠️ 已失效", "grey", "标记失效"),
    }
    title, template, verb = verdicts.get(action, (f"⏰ {action}", "grey", action))

    content = f"**{title_code} {label}** 已由 `{reviewer}` 于 {reviewed_at} {verb}"
    if note:
        content += f"\n**备注**：{note}"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
        ],
    }

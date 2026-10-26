# -*- coding: utf-8 -*-
"""DM 摘要卡可读性施工 (卡②) — 红绿测试。

断言:
1. 行动卡 Top3 措辞中文化: score= → 综合分; 止损 → 参考卖出线 (对齐晚报措辞表);
   审 pending → 审批状态中文 (待审批/已批准/已驳回/已失效/未送审)。
2. 一句话理由 (one_line_reason) 进卡。
3. 无效通报剔除: 无「Q-D4」字样、无「页生成成功」运维计数。
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "marimo_pages"))

import build_marimo_report as B  # noqa: E402


def _signals():
    return pd.DataFrame([
        {"rank": 1, "code": "000001", "name": "测试股", "composite_score": 0.85,
         "buy_range": "9.5~10.0", "stop_loss": 9.0,
         "one_line_reason": "低估值+资金流入", "review_status": "pending",
         "signal_date": "2026-09-20"},
        {"rank": 2, "code": "000002", "name": "示例股", "composite_score": 0.72,
         "buy_range": "8.0~8.5", "stop_loss": 7.5,
         "one_line_reason": "板块热度", "review_status": "approved",
         "signal_date": "2026-09-20"},
    ])


def _build_card():
    outdir = Path("/tmp/qs-test/reports/html/daily/2026-09-20")
    with patch.object(B.C, "ops_oneliner", return_value="运维一行"), \
         patch.object(B.C, "latest_signals", side_effect=_signals):
        return B.build_dm_card("2026-09-20", 7, 7, outdir, period="daily")


def _card_text(card):
    return "\n".join(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")


class TestDmCardChineseWording:
    def test_score_label_chinese(self):
        text = _card_text(_build_card())
        assert "score=" not in text
        assert "综合分" in text

    def test_stop_loss_aligned_with_evening_wording(self):
        text = _card_text(_build_card())
        assert "止损" not in text
        assert "参考卖出线" in text

    def test_review_status_chinese(self):
        text = _card_text(_build_card())
        assert "审 pending" not in text
        assert "待审批" in text
        assert "已批准" in text

    def test_one_line_reason_included(self):
        text = _card_text(_build_card())
        assert "低估值+资金流入" in text


class TestDmCardNoInternalNoise:
    def test_no_qd4_verdict_wording(self):
        card = _build_card()
        text = _card_text(card)
        assert "Q-D4" not in text
        assert "不出机" not in text

    def test_no_page_count_noise(self):
        text = _card_text(_build_card())
        assert "页生成成功" not in text

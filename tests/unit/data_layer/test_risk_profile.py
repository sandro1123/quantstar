"""L1 防雷扩展 RED 夹具 — 风险档案 (adata mine_clearance) 接入 risky codes 面。

派单②: 解禁进 L1 防雷 → 统筹勘正为「风险档案进 L1 防雷」(sm-3f5181e5931d4ae8)。
形态: 选股日批量拉候选股的风险档案 (score/f_type/s_type), 高风险标记码集
供 L1 过滤 — 与现有 get_risky_codes (ST/退市/停牌) 并列成第二风险面。
"""
from unittest.mock import MagicMock, patch

import pytest

from data_layer.risk_profile import (
    RISK_SCORE_GATE,
    RiskProfileService,
)


class TestRiskProfileService:
    def test_high_score_codes_collected(self):
        # score >= 阈值的码进风险集 (按统筹裁定: 阈值施工时按分布定, 先锁默认)
        svc = RiskProfileService(source_registry=MagicMock())
        fake = MagicMock()
        # 000002 score 85 (高于77闸), 600519 score 30 (低于闸) — 09-12 校准后样本
        fake.get_data.side_effect = lambda t, p: (
            [{"code": p["code"], "score": 85.0 if p["code"] == "000002" else 30.0,
              "f_type": "财务类风险", "s_type": "短期负债风险", "t_type": "",
              "reason": "…", "source": "adata"}])
        svc._source = fake
        risky = svc.fetch_risky_codes(["000002", "600519"])
        assert risky == {"000002"}

    def test_empty_candidates_empty_set(self):
        svc = RiskProfileService(source_registry=MagicMock())
        assert svc.fetch_risky_codes([]) == set()

    def test_source_down_degrades_to_none_not_empty(self):
        # fail-closed 语义 (DL-04 同族): 源不可用 → None (调用方必须不放行), 不是空集
        reg = MagicMock()
        reg.get_best_source.return_value = None
        svc = RiskProfileService(source_registry=reg)
        assert svc.fetch_risky_codes(["000002"]) is None

    def test_score_gate_default_locked(self):
        # 默认阈值锁 (真分布账随 09-11 回执呈统筹校准)
        assert RISK_SCORE_GATE == 77  # 09-12 校准: score 中位 77, 用户 GO

    def test_per_call_budget(self):
        # 逐股一调形态 — 调用次数 = 候选数 (拉取量预算账的形态锁)
        svc = RiskProfileService(source_registry=MagicMock())
        fake = MagicMock()
        fake.get_data.side_effect = lambda t, p: []
        svc._source = fake
        svc.fetch_risky_codes([f"60000{i}" for i in range(5)])
        assert fake.get_data.call_count == 5

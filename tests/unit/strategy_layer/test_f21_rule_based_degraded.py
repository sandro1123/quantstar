"""TDD: F-21 第三路径 (规则降级腿) 补齐 — 降级标识显形 + 不改放行口径.

统筹裁定 (sm-c789b802b7b145e4):
  改「降级标识」不改「放行口径」
  - deep_review._rule_based_review 产出的结果 degraded_fallback=True (与 _default_result 同族)
  - 不改 signal_gen 仓位口径 / 不改 confidence 数值 / 不改 gate 阈值 <=0.3

三钉:
  钉1: 规则降级腿产出的结果 degraded_fallback 必须为 True (低置信必 True)
  钉2: 降级票 (观望形态 = 10-20 真实形态) 进 gate 不得被剔除, 且必须出降级 warning
  钉3: 规则降级腿给出的「真实看空」(verdict=不推荐) 即便 degraded_fallback=True,
       也**不得进入推荐列表** —— 降级标识只影响「要不要告警」, 不影响「要不要买」

⚠️ 钉2/钉3 的 fixture 之分 (开发②席 10-21 澄清, 报统筹备案):
  - 钉2 用 verdict="观望" —— 这才是 10-20 真实形态 (fallback_response 返 verdict=观望),
    期望「保底放行 + 告警」。
  - 钉3 用 verdict="不推荐" —— 真实看空, 期望「即便降级也剔除, 不得进推荐」。
  两者不冲突: 剔除判据按「明确看空词表」而非「一切非推荐」。
"""

from unittest.mock import MagicMock

import agent_layer.debate.experts as experts_mod
from strategy_layer import StrategyLayer, StrategyStageContext
from strategy_layer.agent_gate.deep_review import AgentReviewResult, DeepReview
from strategy_layer.aggregation.aggregator import FactorDaily
from strategy_layer.selection.selector import SelectionResult
from strategy_layer.signals.generator import SignalGenerator


def _rule_based(conf_value: float, score: float):
    """构造规则降级腿 (专家腿给定的置信/打分)."""
    class _Rep:
        def __init__(self):
            self.confidence = conf_value
            self.score = score
            self.expert_name = "stub"
            self.key_points = ["p"]

    class _Exp:
        def analyze(self, data):
            return _Rep()

    orig = experts_mod.ALL_EXPERTS
    experts_mod.ALL_EXPERTS = [_Exp]
    dr = DeepReview(experts=[_Exp])
    try:
        return dr._rule_based_review("600001", "2026-10-20",
                                     factor_scores={"momentum_20d": -1.0})
    finally:
        experts_mod.ALL_EXPERTS = orig


# ───────────────────── 钉1: 规则降级腿必须带降级标识 ─────────────────────
def test_rule_based_review_low_confidence_marks_degraded():
    """钉1: 规则降级腿 confidence<=0.3 时 degraded_fallback 必须为 True."""
    res = _rule_based(conf_value=1.0, score=-1.0)
    assert res.confidence <= 0.3, f"夹具前提: 期望低置信, 实得 {res.confidence}"
    assert res.degraded_fallback is True, (
        f"规则降级腿低置信必须标 degraded_fallback=True; 实得 {res.degraded_fallback}")


def test_rule_based_review_any_confidence_marks_degraded():
    """钉1 语义: 规则腿本身就是「LLM 未跑成」的降级产物, 无论置信高低都须标降级 ——
    降级标识的含义是「这是降级产物, 别当业务结论信」, 不是「置信低」."""
    res = _rule_based(conf_value=1.0, score=1.0)
    assert res.confidence > 0.3, f"夹具前提: 期望高置信, 实得 {res.confidence}"
    assert res.degraded_fallback is True, (
        f"规则腿即降级产物, 须标 True; 实得 {res.degraded_fallback}")


# ───────────────────── 钉2: 降级票 (观望形态) 不得被剔除 + 必须告警 ─────────────────────
def test_gate_keeps_degraded_watch_candidate_and_warns(caplog):
    """钉2: degraded_fallback=True + verdict=观望 (10-20 真实形态) 的票
    不得被 gate 剔除, 且必须出降级 warning 留痕."""
    cands = [
        FactorDaily(code="600001", date="2026-10-20", composite_score=1.0, rank=1),
        FactorDaily(code="600002", date="2026-10-20", composite_score=0.9, rank=2),
    ]
    ctx = StrategyStageContext(date="2026-10-20")
    ctx.selection = SelectionResult(date="2026-10-20", candidates=list(cands))

    reviews = [
        AgentReviewResult(
            code="600001", date="2026-10-20", overall_score=0.3, confidence=0.3,
            reasoning="超时降级「未调LLM」", verdict="观望",
            degraded_fallback=True,
        ),
    ]
    mock_dr = MagicMock()
    mock_dr.review_candidates.return_value = reviews
    strat = StrategyLayer()
    strat.deep_review = mock_dr

    with caplog.at_level("WARNING", logger="strategy"):
        strat._stage_agent_gate(ctx)

    remaining = [c.code for c in ctx.selection.candidates]
    assert "600001" in remaining, (
        f"降级票不得被 gate 剔除; 实得剩余 {remaining}")
    warn_text = "\n".join(r.getMessage() for r in caplog.records
                          if r.levelname in ("WARNING", "ERROR"))
    assert "600001" in warn_text, (
        f"降级必须出 warning 留痕 (含码); 实得日志: {warn_text[:300]}")


def test_gate_still_rejects_genuine_low_confidence():
    """钉2 反向: 业务真实低置信 (degraded_fallback=False) 仍被正常剔除."""
    c1 = FactorDaily(code="600001", date="2026-10-20", composite_score=1.0, rank=1)
    c2 = FactorDaily(code="600002", date="2026-10-20", composite_score=0.8, rank=2)
    ctx = StrategyStageContext(date="2026-10-20")
    ctx.selection = SelectionResult(date="2026-10-20", candidates=[c1, c2])

    reviews = [
        AgentReviewResult(
            code="600001", date="2026-10-20", overall_score=0.8, confidence=0.8,
            reasoning="良好", verdict="推荐", degraded_fallback=False,
        ),
        AgentReviewResult(
            code="600002", date="2026-10-20", overall_score=0.2, confidence=0.2,
            reasoning="基本面恶化", verdict="不推荐", degraded_fallback=False,
        ),
    ]
    mock_dr = MagicMock()
    mock_dr.review_candidates.return_value = reviews
    strat = StrategyLayer()
    strat.deep_review = mock_dr
    strat._stage_agent_gate(ctx)

    assert [c.code for c in ctx.selection.candidates] == ["600001"], (
        f"真实低置信必须仍被剔除; 实得 {[c.code for c in ctx.selection.candidates]}")


# ───────────────────── 钉3: 真实看空不得进入推荐列表 ─────────────────────
def test_degraded_bearish_verdict_not_recommended():
    """钉3: 规则降级腿 verdict=不推荐 —— 即便 degraded_fallback=True,
    该票也不得进入推荐列表 (降级标识只影响「要不要告警」, 不影响「要不要买」)."""
    cands = [
        FactorDaily(code="600001", date="2026-10-20", composite_score=1.5, rank=1),
        FactorDaily(code="600002", date="2026-10-20", composite_score=1.2, rank=2),
    ]
    ctx = StrategyStageContext(date="2026-10-20")
    ctx.selection = SelectionResult(date="2026-10-20", candidates=list(cands))

    reviews = [
        AgentReviewResult(
            code="600001", date="2026-10-20", overall_score=0.8, confidence=0.8,
            reasoning="规则式降级分析: 全负因子", verdict="不推荐",
            degraded_fallback=True,
        ),
        AgentReviewResult(
            code="600002", date="2026-10-20", overall_score=0.8, confidence=0.8,
            reasoning="规则式降级分析: 正因子", verdict="推荐",
            degraded_fallback=True,
        ),
    ]
    mock_dr = MagicMock()
    mock_dr.review_candidates.return_value = reviews
    strat = StrategyLayer()
    strat.deep_review = mock_dr
    strat._stage_agent_gate(ctx)

    gen = SignalGenerator()
    signals = gen.generate(ctx.selection)
    buy_codes = {s.code for s in signals if s.action == "buy"}

    assert "600001" not in buy_codes, (
        f"真实看空票即便 degraded=True 也不得进入推荐列表; 实得 buy={buy_codes}")
    assert "600002" in buy_codes, (
        f"看多票应正常进入推荐; 实得 buy={buy_codes}")

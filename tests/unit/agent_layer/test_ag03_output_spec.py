"""AG-03: OutputSpec 契约 + verdict 词表统一 测试"""
from agent_layer.core.output_spec import (
    ALLOWED_VERDICTS, OUTPUT_SPECS, normalize_verdict, validate_output_spec,
)


def test_allowed_verdicts_enum():
    assert ALLOWED_VERDICTS == ("推荐", "不推荐", "观望", "警惕")


def test_output_specs_cover_all_task_types():
    for task in ("stock_analysis", "market_analysis", "entry_check", "exit_review"):
        assert task in OUTPUT_SPECS


def test_normalize_legacy_vocab():
    """旧词表归一化: approve/reject/关注/买入/卖出/neutral → 统一枚举"""
    assert normalize_verdict("approve") == "推荐"
    assert normalize_verdict("reject") == "不推荐"
    assert normalize_verdict("关注") == "推荐"
    assert normalize_verdict("买入") == "推荐"
    assert normalize_verdict("卖出") == "不推荐"
    assert normalize_verdict("neutral") == "观望"
    assert normalize_verdict("hold") == "观望"
    assert normalize_verdict("watch") == "观望"


def test_normalize_unknown_falls_back_to_wait():
    """未知 verdict 诚实落观望, 不伪造方向"""
    assert normalize_verdict("随便什么") == "观望"
    assert normalize_verdict("") == "观望"
    assert normalize_verdict(None) == "观望"


def test_normalize_idempotent_on_unified():
    for v in ALLOWED_VERDICTS:
        assert normalize_verdict(v) == v


def test_validate_flags_off_enum_verdict():
    warnings = validate_output_spec("stock_analysis", {"verdict": "approve", "confidence": 0.5, "reasoning": "x"})
    assert any("verdict" in w for w in warnings)


def test_validate_flags_bad_confidence():
    warnings = validate_output_spec("stock_analysis", {"verdict": "推荐", "confidence": 1.5, "reasoning": "x"})
    assert any("confidence" in w for w in warnings)


def test_validate_flags_missing_field():
    warnings = validate_output_spec("stock_analysis", {"verdict": "推荐", "confidence": 0.7})
    assert any("reasoning" in w for w in warnings)


def test_validate_clean_contract_no_warnings():
    warnings = validate_output_spec("stock_analysis", {"verdict": "推荐", "confidence": 0.7, "reasoning": "因子向好"})
    assert warnings == []


def test_entry_check_agent_unified_verdict():
    """EntryCheckAgent 直出统一词表 (不再用不存在的字段构造 AgentResult)"""
    from agent_layer.agents.entry_check_agent import EntryCheckAgent
    ok = EntryCheckAgent().analyze({"code": "000001", "entry_price": 15.0})
    assert ok.verdict == "推荐"
    bad = EntryCheckAgent().analyze({"code": "ST0001", "entry_price": 15.0})
    assert bad.verdict == "不推荐"
    assert bad.risk_flags


def test_exit_review_agent_unified_verdict():
    """ExitReviewAgent 直出统一词表"""
    from agent_layer.agents.exit_review_agent import ExitReviewAgent
    profit = ExitReviewAgent().analyze({"code": "000001", "exit_reason": "take_profit", "pnl_pct": 5.0, "hold_days": 3})
    assert profit.verdict == "推荐"
    loss = ExitReviewAgent().analyze({"code": "000001", "exit_reason": "stop_loss", "pnl_pct": -3.0, "hold_days": 2})
    assert loss.verdict == "不推荐"
    noreason = ExitReviewAgent().analyze({"code": "000001", "exit_reason": "", "pnl_pct": 1.0, "hold_days": 2})
    assert noreason.verdict == "警惕"
    assert "missing_exit_reason" in noreason.risk_flags


def test_agent_layer_quick_scan_unified_verdict():
    """analyze() 出口: quick_scan 高 composite → 推荐 (不再是旧词表 关注)"""
    from common.database import Database
    from agent_layer import AgentLayer, AgentRequest
    db = Database(":memory:")
    al = AgentLayer(database=db, config={})
    req = AgentRequest(task="stock_analysis",
                       input={"code": "000001", "date": "2026-08-18",
                              "factor_scores": {"f1": 0.9, "f2": 0.9}},
                       depth="quick_scan", consumer="test")
    resp = al.analyze(req)
    assert resp.verdict in ALLOWED_VERDICTS
    assert resp.verdict == "推荐"
    db.close()


def test_agent_layer_normalizes_off_enum_verdict():
    """analyze() 出口归一化: 枚举外 verdict 收敛到统一词表"""
    from unittest.mock import patch as mock_patch
    from common.database import Database
    from agent_layer import AgentLayer, AgentRequest
    from agent_layer.core.agent_layer import AgentLayer as AL
    db = Database(":memory:")
    al = AgentLayer(database=db, config={})
    req = AgentRequest(task="stock_analysis", input={"code": "000002", "date": "2026-08-18"},
                       consumer="test")
    # mock 管线执行返回旧词表 approve, 验证 analyze() 出口归一化
    with mock_patch.object(AL, "_execute_pipeline") as ep:
        from agent_layer.core.models import AgentResponse
        ep.return_value = AgentResponse(request_id="t", task="stock_analysis", code="000002",
                                        verdict="approve", confidence=0.7, reasoning="x")
        resp = al.analyze(req)
    assert resp.verdict == "推荐"
    db.close()

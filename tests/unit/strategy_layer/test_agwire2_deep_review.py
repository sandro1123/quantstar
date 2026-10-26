"""AG-WIRE-2 (2026-09-08): DeepReview 深审门 top-N 升档三面测试。

修复点: deep_review.review_candidates 原对全候选发 depth="normal" 单腿
(零候选走过六专家辩论链), 与类名 DeepReview/注册名「深审门」名实不符。
修: 每日真正要建仓的前 deep_top_n (默认 5) 只候选 (按候选自带分数字段
composite_score 降序) 升 depth="deep" → 路由 deep_debate 管线 (六专家
+裁决官), 其余维持 normal; 升档请求显式带 AG-COMPOSE 三新键
style="risk_officer" / focus=["entry_risk", "factor_persistence"] /
max_words=300 (建仓审核面)。

三面:
① top-5 升档 + 其余 normal 的分流断言
② config 开关 strategy.agent_gate.deep_top_n=0 时全 normal
③ depth="deep" 请求三新键穿到专家模板的断言 (QS_LLM_OFFLINE=1 离线腿,
   以 _run_debate 组装的 expert_data 为证面)

成本面 (卡注释, 统筹段口径): 每日约 5 次 deep 腿 × 6 专家 ≈ 35 次
LLM 调用/天; cbcn 优先池单次 4~7s; token_budget multi_agent_review/deep
预算面沿用 deep_debate 既有档 (3000/120s), 不新设预算桶。
"""
import pytest
from unittest.mock import MagicMock

from strategy_layer.agent_gate.deep_review import DeepReview
from strategy_layer.aggregation.aggregator import FactorDaily

DATE = "2026-09-08"


def _make_candidates(n: int) -> list:
    """n 只候选, composite_score 0.1..0.n — 排序键用候选自带分数字段。"""
    return [
        FactorDaily(code=f"6000{i:02d}", date=DATE, composite_score=0.1 * (i + 1))
        for i in range(n)
    ]


# deep_top_n 默认值锚（引用生产常量，用户 09-25 裁 5→10；再调值测试零改动）
_N = DeepReview._DEFAULT_DEEP_TOP_N


def _mock_agent_layer() -> MagicMock:
    """记录每次 analyze 收到的 AgentRequest, 返回可过闸 (confidence>0.3) 回执。"""
    mock = MagicMock()

    def analyze(req):
        mock.requests.append(req)
        return MagicMock(confidence=0.8, reasoning="ok", risk_flags=[])

    mock.requests = []
    mock.analyze.side_effect = analyze
    return mock


class TestDeepTopNSplit:
    """面①: top-N 升档 deep、其余 normal 的分流"""

    def test_top5_upgraded_rest_normal(self):
        # 语义: composite_score 最高的 N 只升 deep, 其余维持 normal
        # (N = 生产默认 deep_top_n; 候选数取 N+3 保证有 normal 对照面)
        mock = _mock_agent_layer()
        reviewer = DeepReview(agent_layer=mock)
        total = _N + 3
        results = reviewer.review_candidates(_make_candidates(total), DATE)

        assert len(results) == total
        depths = {r.code: req.depth for r, req in zip(results, mock.requests)}
        deep_codes = {c.code for c in sorted(
            _make_candidates(total), key=lambda x: x.composite_score, reverse=True)[:_N]}
        for code, depth in depths.items():
            if code in deep_codes:
                assert depth == "deep", f"{code} 应升 deep 档"
            else:
                assert depth == "normal", f"{code} 应维持 normal 档"

    def test_fewer_than_top_n_all_deep(self):
        # 候选数 < top_n 时全量升档, 不许有空档漏审
        mock = _mock_agent_layer()
        reviewer = DeepReview(agent_layer=mock)
        reviewer.review_candidates(_make_candidates(3), DATE)
        assert all(req.depth == "deep" for req in mock.requests)
        assert len(mock.requests) == 3

    def test_top_n_order_by_composite_score_not_input_order(self):
        # 输入乱序: 升档必须按 composite_score 取 top, 不按传入顺序
        mock = _mock_agent_layer()
        reviewer = DeepReview(agent_layer=mock)
        total = _N + 2
        cands = _make_candidates(total)
        shuffled = list(reversed(cands[: total // 2])) + list(reversed(cands[total // 2:]))
        reviewer.review_candidates(shuffled, DATE)
        deep_codes = {req.input["code"] for req in mock.requests if req.depth == "deep"}
        # 期望 = 按 composite_score 取 top-N（与输入顺序无关）
        expect = {c.code for c in sorted(
            cands, key=lambda x: x.composite_score, reverse=True)[:_N]}
        assert deep_codes == expect

    def test_deep_request_carries_three_new_keys(self):
        # 升档请求显式带 AG-COMPOSE 三新键 (建仓审核面)
        mock = _mock_agent_layer()
        reviewer = DeepReview(agent_layer=mock)
        reviewer.review_candidates(_make_candidates(6), DATE)
        for req in mock.requests:
            if req.depth == "deep":
                assert req.style == "risk_officer"
                assert req.focus == ["entry_risk", "factor_persistence"]
                assert req.max_words == 300
            else:
                # normal 档维持默认, 不越权改请求规范
                assert req.style == "professional_advisor"
                assert req.focus == []
                assert req.max_words == 200

    def test_default_top_n_is_5(self):
        # 缺省 deep_top_n = 生产常量 (strategy.agent_gate.deep_top_n 可配)
        # 用户 09-25 裁 5→10: 治「非 topN 票理由永远占位」，扩到全覆盖推荐面
        mock = _mock_agent_layer()
        reviewer = DeepReview(agent_layer=mock)
        reviewer.review_candidates(_make_candidates(_N + 3), DATE)
        assert sum(1 for req in mock.requests if req.depth == "deep") == _N


class TestDeepTopNConfigSwitch:
    """面②: config 开关 strategy.agent_gate.deep_top_n"""

    def test_config_zero_disables_deep(self):
        mock = _mock_agent_layer()
        reviewer = DeepReview(agent_layer=mock, config={"strategy.agent_gate.deep_top_n": 0})
        reviewer.review_candidates(_make_candidates(8), DATE)
        assert all(req.depth == "normal" for req in mock.requests)

    def test_config_overrides_default(self):
        mock = _mock_agent_layer()
        reviewer = DeepReview(agent_layer=mock, config={"strategy.agent_gate.deep_top_n": 2})
        reviewer.review_candidates(_make_candidates(8), DATE)
        assert sum(1 for req in mock.requests if req.depth == "deep") == 2

    def test_config_object_get_supported(self):
        # 生产接线是 config 对象 (Config.get(key, default)) — 与 dict 双形态兼容
        mock = _mock_agent_layer()
        cfg = MagicMock()
        cfg.get.side_effect = lambda key, default=None: (
            3 if key == "strategy.agent_gate.deep_top_n" else default)
        reviewer = DeepReview(agent_layer=mock, config=cfg)
        reviewer.review_candidates(_make_candidates(8), DATE)
        assert sum(1 for req in mock.requests if req.depth == "deep") == 3

    def test_negative_or_invalid_falls_back_to_default(self):
        # 非法配置 (负数) 不许全量升档烧预算 — 回落生产默认值
        mock = _mock_agent_layer()
        reviewer = DeepReview(agent_layer=mock, config={"strategy.agent_gate.deep_top_n": -1})
        reviewer.review_candidates(_make_candidates(_N + 3), DATE)
        assert sum(1 for req in mock.requests if req.depth == "deep") == _N


class TestDeepKeysReachExpertTemplates:
    """面③: depth="deep" 请求三新键穿到专家模板 (QS_LLM_OFFLINE=1 离线腿)"""

    def test_run_debate_propagates_style_focus_max_words(self, monkeypatch):
        # 直接在 agent_layer._run_debate 的专家装配面上断言: DeepReview 发出的
        # 三新键经 expert_data 专位 (style_hint/focus/max_words) 穿到专家模板。
        # 拦截 SentimentExpert.analyze 抓 expert_data — 离线腿必走规则腿,
        # LLM 不可用不阻断本断言 (data 装配先于 LLM 调用)。
        from agent_layer.core.agent_layer import AgentLayer
        from agent_layer.core.models import AgentRequest
        from common.database import Database
        from agent_layer.debate import experts as experts_mod

        captured = {}
        orig_analyze = experts_mod.SentimentExpert.analyze

        def spy_analyze(self, data):
            captured.update(data)
            return orig_analyze(self, data)

        monkeypatch.setattr(experts_mod.SentimentExpert, "analyze", spy_analyze)

        db = Database(":memory:")
        al = AgentLayer(database=db, config={})
        try:
            # DeepReview 实发请求的镜像: depth=deep + 三新键
            req = AgentRequest(
                task="stock_analysis",
                input={"code": "600519", "date": DATE,
                       "factor_scores": {"momentum_20d": 0.8}},
                depth="deep",
                style="risk_officer",
                focus=["entry_risk", "factor_persistence"],
                max_words=300,
            )
            resp = al.analyze(req)
            assert resp.verdict in ("推荐", "不推荐", "观望")

            # 三新键穿到专家腿: focus/max_words 专位 + style→style_hint 风控官文案
            assert captured.get("focus") == "entry_risk; factor_persistence"
            assert captured.get("max_words") == 300
            assert "风控官" in captured.get("style_hint", "")
        finally:
            db.close()

    def test_deep_routes_to_deep_debate_pipeline(self):
        # 铆钉: depth="deep" 的 stock_analysis 请求由 registry 路由到 deep_debate
        # (六专家+裁决官) — 这是升档的先决条件, 防未来 registry 变动静默脱钩
        from agent_layer.core.registry import get_default_registry
        reg = get_default_registry()
        selected = None
        try:
            selected = reg.select("stock_analysis", "deep")
        except Exception:
            tbl = getattr(reg, "_pipelines", {})
            for ps in tbl.values():
                for p in (ps if isinstance(ps, list) else [ps]):
                    if p.task == "stock_analysis" and p.depth == "deep":
                        selected = p
        assert selected is not None and selected.name == "deep_debate"

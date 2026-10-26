"""DeepReview — 调用 AgentLayer 对选股结果进行深度分析，支持规则式降级"""
from dataclasses import dataclass, field

from common.logging import get_logger
from agent_layer import build_request

logger = get_logger("strategy.agent_gate")


def _fetch_stock_news(code: str, date: str, data_layer=None) -> str:
    """DL-41 + DL-71: 个股新闻摘要。

    先读库 (get_stock_news), 只取 code 精确匹配的**个股**新闻 ——
    注意 get_stock_news 会用市场级新闻 (code='') 补足, 不能算个股新闻
    (DL-66 普查: stock_news.code 全空串, 个股新闻结构性缺失)。
    库无个股新闻时经 data_layer 读 API 现场按股拉取 (DL-71 按需取)。
    **策略层不直连 akshare** (DL-41 边界), 现场取数由
    data_layer.get_stock_news_live 承担。两路皆空诚实返回空字符串, 不造假值。
    """
    if data_layer is None:
        return ""
    titles: list[str] = []
    try:
        items = data_layer.get_stock_news(code, date, limit=10) or []
        # 只认个股新闻: code 精确匹配才算; code='' 的市场级新闻不算
        # (get_stock_news 会用市场新闻补足, 不能当个股新闻)。
        # 无 code 字段的项 (旧调用方) 视为匹配, 保持 DL-41 前向兼容。
        titles = [str(it.get("title", "")) for it in items
                  if it.get("title") and it.get("code", code) == code]
    except Exception:
        titles = []
    if not titles:
        # DL-71: 库无个股新闻 (结构性缺失), 现场按股拉取 (data_layer 承担 akshare)。
        live = getattr(data_layer, "get_stock_news_live", None)
        if callable(live):
            try:
                live_items = live(code, limit=10) or []
                titles = [str(it.get("title", "")) for it in live_items if it.get("title")]
            except Exception:
                titles = []
    if not titles:
        return ""
    return "个股新闻: " + "; ".join(titles)[:500]


@dataclass
class AgentReviewResult:
    code: str
    date: str
    overall_score: float
    confidence: float
    reasoning: str
    # P0-2 完整版 (2026-10-04): verdict 闸恢复必备 — agent 推荐/观望/不推荐
    # 词表对齐 output_spec.normalize_verdict (LLM_推荐 → "推荐"...);
    # 由 review_candidate (LLM 路径) / _rule_based_review (规则降级) /
    # _default_result (诚实降级) 三处真填。空串 = 字段缺 (第十变体判死区)。
    verdict: str = ""
    risk_flags: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    # AG-NEWS ⑤: 分级后个股新闻 (tier<=3, 噪声滤除) — 第七专家新闻面
    news_items: list[dict] = field(default_factory=list)
    # F-21: 系统性故障/未调LLM兜底标记 (防信心<=0.3误杀全灭)
    degraded_fallback: bool = False


class DeepReview:
    # AG-WIRE-2 (2026-09-08): 成本面 — 每日约 deep_top_n (默认 10, 09-25 用户裁 5→10) 次 deep 腿
    # × 6 专家 ≈ 35 次 LLM 调用/天; cbcn 优先池单次 4~7s; token_budget
    # multi_agent_review/deep 预算面沿用 deep_debate 既有档 (3000/120s),
    # 不新设预算桶。升档候选走 deep_debate 管线 (六专家+多空辩+裁决官)。
    DEEP_STYLE = "risk_officer"           # 建仓审核面: 风控官风格
    DEEP_FOCUS = ["entry_risk", "factor_persistence"]
    DEEP_MAX_WORDS = 300
    # 2026-09-25 用户裁定 5→10: 辩论只覆盖 deep_top_n 只, 其余票的推荐理由
    # 永远是「生成中」占位 (用户亲自发现晚报选股理由全是占位)。扩到 10 才能
    # 全覆盖当日推荐面。成本已核: 10只×6专家≈14积分/天 vs 池回血 300/日。
    _DEFAULT_DEEP_TOP_N = 10

    def __init__(self, agent_layer=None, experts=None, trace_id: str | None = None,
                 data_layer=None, config=None):
        self.agent_layer = agent_layer
        self.experts = experts
        self._trace_id = trace_id or ""
        # DL-41: 新闻取数经 DataLayer 读 API (收编外部库直连)
        self.data_layer = data_layer
        # AG-WIRE-2: config 开关 strategy.agent_gate.deep_top_n (默认 10, 09-25 裁定)。
        # 兼容 dict / 带 .get(key, default) 的 config 对象双形态。
        self.config = config

    def _deep_top_n(self) -> int:
        raw = None
        try:
            if isinstance(self.config, dict):
                raw = self.config.get("strategy.agent_gate.deep_top_n")
            elif self.config is not None:
                raw = self.config.get("strategy.agent_gate.deep_top_n", None)
        except Exception:
            raw = None
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return self._DEFAULT_DEEP_TOP_N
        if n < 0:
            return self._DEFAULT_DEEP_TOP_N  # 非法值回落默认, 不许全量升档烧预算
        return n

    def review_candidate(self, code: str, date: str, factor_scores: dict | None = None,
                         deep: bool = False) -> AgentReviewResult:
        try:
            if self.agent_layer is None:
                raise ValueError("AgentLayer not available")


            # 获取新闻摘要
            news_summary = _fetch_stock_news(code, date, self.data_layer)
            # AG-NEWS ⑤: 个股新闻分级装配 (公告>异动>题材; 噪声不进专家脑)。
            # 读库 code 精确匹配行, derived_title_match 派生关联也入 (弱证据
            # 由模板降权说明)。分级失败不阻断 (空 items 诚实降级)。
            news_items: list[dict] = []
            try:
                from data_layer.news_tiering import classify_news_tier
                if self.data_layer is not None:
                    raw_items = self.data_layer.get_stock_news(code, date, limit=10) or []
                    own = [it for it in raw_items
                           if it.get("title") and it.get("code", code) == code]
                    for it in own:
                        t = classify_news_tier(str(it.get("title", "")),
                                               str(it.get("summary", "")))
                        if t["tier"] <= 3:
                            news_items.append({
                                "title": str(it.get("title", "")),
                                "tier": t["tier"], "label": t["label"],
                                "signals": t["signals"],
                                "method": it.get("attribution_method", ""),
                            })
            except Exception as _tier_e:
                logger.debug(f"news tiering skipped: {_tier_e}")

            if deep:
                # AG-WIRE-2: 升档请求显式带 AG-COMPOSE 三新键 (建仓审核面) —
                # style/focus/max_words 经 _run_debate 翻成专家模板专位
                # (style_hint/focus/max_words), 已有链路, 不另设装配点。
                request = build_request(
                    task="stock_analysis",
                    input={
                        "code": code,
                        "factor_scores": factor_scores or {},
                        "date": date,
                        "news_summary": news_summary,
                        "news_items": news_items,
                    },
                    depth="deep",
                    style=self.DEEP_STYLE,
                    focus=self.DEEP_FOCUS,
                    max_words=self.DEEP_MAX_WORDS,
                )
            else:
                request = build_request(
                    task="stock_analysis",
                    input={
                        "code": code,
                        "factor_scores": factor_scores or {},
                        "date": date,
                        "news_summary": news_summary,
                        "news_items": news_items,
                    },
                    depth="normal",
                )
            response = self.agent_layer.analyze(request)

            return AgentReviewResult(
                code=code,
                date=date,
                overall_score=response.confidence,
                confidence=response.confidence,
                reasoning=response.reasoning,
                # P0-2 完整版: LLM 路径真填 response.verdict (output_spec 已
                # normalize_verdict 收敛统一词表) — 闸拒+拼接都靠它。
                verdict=response.verdict,
                risk_flags=[f.flag for f in response.risk_flags] if response.risk_flags else [],
                suggestions=[],
                news_items=news_items,
                degraded_fallback=getattr(response, "degraded_fallback", False),
            )
        except Exception as e:
            logger.warning(f"Agent review failed for {code}: {e}")
            try:
                return self._rule_based_review(code, date, factor_scores)
            except Exception as e2:
                logger.warning(f"Rule-based review also failed for {code}: {e2}")
                return self._default_result(code, date)

    def _rule_based_review(self, code: str, date: str, factor_scores: dict | None = None) -> AgentReviewResult:
        experts = self.experts
        if experts is None:
            from agent_layer.debate.experts import ALL_EXPERTS
            experts = ALL_EXPERTS

        data: dict = {"code": code, "date": date}
        if factor_scores:
            # Map strategy factor names to expert-expected keys
            _FACTOR_TO_EXPERT = {
                "volatility_reversal": "momentum_20d",
                "std20": "volatility_20d",
                "cntp5": "volume_change",
                "vma20": "volume_ratio",
                "alpha_48": "pe",
                "momentum_20d": "momentum_20d",
                "roe_quality": "roe",
                "sump20": "volume_ratio",
                "corr5": "volume_change",
                "bb_width": "volatility_20d",
                "turnover_ratio": "turnover_ratio",
                "value_discount": "pe",
                "size_small_cap": "market_cap",
                "liquidity_illiquid": "volume_ratio",
                "seasonality_same_month": "momentum_20d",
            }
            for factor_name, value in factor_scores.items():
                expert_key = _FACTOR_TO_EXPERT.get(factor_name, factor_name)
                data[expert_key] = value
            data["composite_score"] = sum(abs(v) for v in factor_scores.values()) / max(1, len(factor_scores))

        reports = [cls().analyze(data) for cls in experts]

        total_weighted = sum(r.score * r.confidence for r in reports)
        total_weight = sum(r.confidence for r in reports)

        if total_weight == 0:
            avg_score = 0.0
        else:
            avg_score = total_weighted / total_weight

        normalized = (avg_score + 0.5) / 1.0
        confidence = max(0.0, min(1.0, normalized))

        reasoning_parts = [
            f"[{r.expert_name}] {'; '.join(r.key_points)} (score={r.score:.2f}, conf={r.confidence:.2f})"
            for r in reports
        ]
        reasoning = "规则式降级分析:\n" + "\n".join(reasoning_parts)

        # P0-2 完整版: 规则路径 verdict 从 confidence 推断 (4 段桶) —
        # 高置信(≥0.7)推荐, 中置信(0.4-0.7)观望, 低置信(<0.4)不推荐。
        # 与 output_spec.normalize_verdict 收敛词表一致。
        if confidence >= 0.7:
            _verdict = "推荐"
        elif confidence >= 0.4:
            _verdict = "观望"
        else:
            _verdict = "不推荐"

        return AgentReviewResult(
            code=code,
            date=date,
            overall_score=confidence,
            confidence=confidence,
            reasoning=reasoning,
            # P0-2 完整版: 规则路径真填 verdict (从 confidence 推断)。
            verdict=_verdict,
            risk_flags=[],
            suggestions=["规则式降级 - 建议结合更多数据人工复核"],
            # F-21 第三路径 (统筹 sm-c789b802b7b145e4): 规则腿本身就是
            # 「LLM 未跑成」的降级产物 —— 显形标记, 别让它冒充业务结论。
            # 与 _default_result / _fallback_response 同族 (三态: True=降级产物)。
            degraded_fallback=True,
        )

    def _default_result(self, code: str, date: str) -> AgentReviewResult:
        return AgentReviewResult(
            code=code, date=date,
            overall_score=0.0, confidence=0.0,
            reasoning="AgentLayer not available",
            # P0-2 完整版: 诚实降级 - 无 agent 可用时 verdict=观望 (不谎称推荐)。
            verdict="观望",
            degraded_fallback=True,
        )

    def review_candidates(self, candidates: list, date: str,
                          must_deep: set | None = None) -> list[AgentReviewResult]:
        """R-2 方案① (10-19): must_deep = 推荐码预算集 (agent_gate 前同构预算)。

        deep_set = composite_score 前 N ∪ must_deep。动机: recommendation_rationale
        只在 deep 辩论腿落库 (agent_layer.py:764), 推荐码 (signal_gen buy) 若被
        前 N 截断在外 → 两表不相交 → 晚报消费面占位话术。判据4 划界: deep_top_n
        默认值与 log 格式不变, 只加并集不加过滤。"""
        must_deep = must_deep or set()
        # AG-WIRE-2 (2026-09-08): 名实归位 — 深审门对真正要建仓的前 top-N 只
        # 候选升 depth="deep" (deep_debate 管线: 六专家+多空辩+裁决官),
        # 其余维持 normal 单腿。排序键 = 候选自带分数字段 composite_score
        # (FactorDaily 真实字段, aggregator.py:54 实证, 非猜测); 无该字段的
        # 裸 code 候选 (str) 不升档, 维持既有 normal 行为。
        top_n = self._deep_top_n()
        deep_set = set()
        if top_n > 0:
            scored = [
                (getattr(c, "composite_score"), c) for c in candidates
                if hasattr(c, "code")
                and isinstance(getattr(c, "composite_score", None), (int, float))
            ]
            scored.sort(key=lambda t: t[0], reverse=True)
            deep_set = {id(c) for _, c in scored[:top_n]}
        # R-2 方案①: 推荐码预算并入 (按 id 对齐 — 同对象判据, 防同码不同实例漏并)
        for c in candidates:
            if hasattr(c, "code") and c.code in must_deep:
                deep_set.add(id(c))
        results = []
        for c in candidates:
            code = c.code if hasattr(c, "code") else c
            scores = getattr(c, "factor_scores", None)
            result = self.review_candidate(code, date, scores, deep=id(c) in deep_set)
            results.append(result)
        n_deep = sum(1 for c in candidates if id(c) in deep_set)
        logger.info(
            f"DeepReview: deep_top_n={top_n}, {n_deep}/{len(results)} candidates "
            f"upgraded to deep_debate; rest stay normal")
        passed = [r for r in results if r.confidence > 0.3]
        logger.info(f"DeepReview: {len(passed)}/{len(results)} candidates passed agent gate")
        return results

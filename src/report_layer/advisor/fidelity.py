# -*- coding: utf-8 -*-
"""忠实性守卫三闸 — 模块1b 核心交付（RECOMMENDATION_COHERENCE_CARD_20260920）

闸1 结论一致：verdict/方向/置信档位加工前后映射等价（白名单函数，含未知/缺失→保守最低档显式分支）
闸2 依据封闭：产出事实性断言/数字/因子名 ⊆ 原料字段集合，差集非空即拒发（幻觉防线）
闸3 可追溯：每句带 source 标注 ∈ 白名单三来源

守卫失败不静默：降级=原始 reasoning 原文 + 术语小注（诚实水位，不编造、不回退排名话术）。
"""
from dataclasses import dataclass
from .models import SOURCE_WHITELIST, RecommendationArgument
from .text_domain import find_numbers, find_factors

# ---------- 闸1：结论一致性 ----------

# verdict 等价类：白名单映射（判例：白名单要含未知/缺失→保守最低档的显式分支）
# 审计复核 P0①（2026-09-21）: 与 AG-03 中文词表同源 —— 1a 落库 verdict 是
# decision_hub 实产的中文（推荐/不推荐/观望/警惕），旧实现只认英文致
# 「推荐 + 文本不宜买入」被误判 -1==-1 而全闸放行（真翻转逃逸）。
_VERDICT_DIRECTION = {
    # 英文（既有路径，回归锚，语义不变）
    "buy": 1, "hold": 0, "avoid": -1,
    # AG-03 中文词表（decision_hub / normalize_verdict 实产）
    "推荐": 1, "买入": 1,
    "不推荐": -1, "卖出": -1, "规避": -1,
    "观望": 0, "警惕": 0, "持有": 0,
    # 未知/缺失/变体一律保守映射为 -1（最低档），禁止 KeyError 穿透、禁止默认中档
}
_CONSERVATIVE_DIRECTION = -1

# 置信档位（三档白名单；未知/缺失→保守最低档「弱」）
_CONFIDENCE_BANDS = ((0.67, "strong"), (0.34, "moderate"), (float("-inf"), "weak"))
_CONSERVATIVE_BAND = "weak"

# 文本方向信号（加工后文本里检测到的方向语义）
_POSITIVE_HINTS = ("买入", "建议买入", "可买入", "值得关注", "看多", "建仓", "强势", "推荐")
_NEGATIVE_HINTS = ("不宜买入", "回避", "卖出", "观望", "看空", "减持", "止损", "离场", "谨慎")
_STRONG_HINTS = ("强烈建议", "强烈推荐", "务必", "大胆")


def _band_of(confidence) -> str:
    """置信→档位白名单映射；None/NaN/未知→保守最低档（显式分支，不静默中档）"""
    try:
        if confidence is None:
            return _CONSERVATIVE_BAND
        c = float(confidence)
    except (TypeError, ValueError):
        return _CONSERVATIVE_BAND
    for threshold, band in _CONFIDENCE_BANDS:
        if c >= threshold:
            return band
    return _CONSERVATIVE_BAND


def _direction_of_verdict(verdict) -> int:
    """verdict→方向白名单映射；未知/缺失→保守 -1"""
    if not verdict:
        return _CONSERVATIVE_DIRECTION
    return _VERDICT_DIRECTION.get(str(verdict).strip().lower(), _CONSERVATIVE_DIRECTION)


def _text_direction(text: str) -> int:
    """从加工文本检测方向语义。
    子串包含修正（判例#18）：负向短语可能内嵌正向子串（'不宜买入'⊃'买入'），
    先按长词优先命中负向短语并屏蔽其区间，再扫正向词——否则 '不宜买入' 被同时判为
    正负命中 → 方向0 → 翻转结论逃过闸1（挡'该股动量强势'与'综合判断当前不宜买入'）。"""
    masked = text
    neg_hits = [h for h in _NEGATIVE_HINTS if h in masked]
    for h in neg_hits:
        masked = masked.replace(h, "□" * len(h))
    pos_hits = [h for h in _POSITIVE_HINTS if h in masked]
    if pos_hits and neg_hits:
        return 0
    if pos_hits:
        return 1
    if neg_hits:
        return -1
    return 0


def _text_strength(text: str) -> int:
    return 1 if any(h in text for h in _STRONG_HINTS) else 0


# ---------- 守卫主类 ----------

@dataclass
class GuardVerdict:
    ok: bool
    gate: str = ""          # "conclusion" / "evidence" / "trace" / ""
    detail: str = ""


@dataclass
class DegradedOutput:
    degraded: bool
    text: str = ""


class FidelityGuard:
    """加工产出的忠实性守卫。原料 = RecommendationArgument（不可变语义锚）"""

    def __init__(self, argument: RecommendationArgument):
        self.arg = argument

    # -- 闸1 --
    def _check_conclusion(self, sentences) -> GuardVerdict:
        want_dir = _direction_of_verdict(self.arg.verdict)
        want_band = _band_of(self.arg.confidence)
        for s in sentences:
            text = s.text or ""
            td = _text_direction(text)
            # 方向翻转（含软化翻转）。审计同族第4处：原实现 `want_dir != 0` 使
            # 「观望(0) → 买入(+1)」也短路放行——中性原料被加工成明确方向同样是改结论。
            # 仅当文本方向不可判定(td==0)时才不判（无可判信号，交闸2/3）。
            if td != 0 and td != want_dir:
                return GuardVerdict(False, "conclusion",
                                    f"方向翻转: 原料verdict={self.arg.verdict}({want_dir}), 文本方向={td}")
            # 置信档位升格
            if _text_strength(text) and want_band != "strong":
                return GuardVerdict(False, "conclusion",
                                    f"置信升格: 原料档位={want_band}, 文本含强推措辞")
            # strong 文本方向必须与原料方向一致
            if _text_strength(text) and want_dir != 1:
                return GuardVerdict(False, "conclusion",
                                    f"强推措辞与原料方向不符: verdict={self.arg.verdict}")
        return GuardVerdict(True)

    # -- 闸2 --
    def _check_evidence(self, sentences) -> GuardVerdict:
        # 指示词前缀剥离：'该股动量因子'→'动量因子'，防 regex 把指示词并进因子名（判例：字符串包含判定≠语义判定）
        _DEMONSTRATIVES = ("该股", "该", "此股", "此", "本股", "本")
        # claims = 原料的合法扩展域（统筹 09-21 加固）：1a claims JSON 的
        # text 并入原料锚、source.ref 并入因子白名单、value 并入数字白名单。
        # 它是扩展域而非可绕过面——claims 本身已过 1a 出口硬闸（每句可回溯）。
        claims_text_parts, claims_refs, claims_values = [], [], []
        for c in (self.arg.claims or []):
            if not isinstance(c, dict):
                continue
            t = c.get("text") or ""
            if t:
                claims_text_parts.append(t)
            src = c.get("source") or {}
            if isinstance(src, dict):
                if src.get("ref"):
                    claims_refs.append(str(src["ref"]))
                if src.get("value") is not None:
                    claims_values.append(str(src["value"]))
        for s in sentences:
            text = s.text or ""
            # 编造依据话术族先查（更强的编造信号，先于数字短路）
            for phrase in ("历史战绩", "历史回测", "回测显示", "胜率", "过去推荐", "以往推荐"):
                if phrase in text:
                    return GuardVerdict(False, "evidence",
                                        f"凭空依据: '{phrase}' 原料无此依据（战绩属模块3产出，1b禁编）")
            # 编造检测：数字/百分比/价格出现在产出但原料无（数字白名单=numbers+claims.value）
            # P1①：口径统一到 text_domain.find_numbers（原为本地 regex，与 source 侧不等宽）
            produced_numbers = find_numbers(text)
            number_allowlist = list(self.arg.numbers or []) + claims_values
            for p in produced_numbers:
                if not any(p in (n or "") for n in number_allowlist):
                    return GuardVerdict(False, "evidence",
                                        f"凭空数字: '{p}' 不在原料 numbers={self.arg.numbers}")
            # 编造因子：文本含「XX因子」但原料无（先剥指示词；封闭域=原料全文+claims，
            # 因 factors 存英文 id 而产出用中文名，原料原文里出现过的名字即被背书）
            material_text = " ".join(
                [self.arg.reasoning or ""]
                + list(self.arg.key_points or [])
                + list(self.arg.invalidation_conditions or [])
                + claims_text_parts
            )
            factor_allowlist = list(self.arg.factors or []) + claims_refs
            for m in find_factors(text):
                name = m.group(1)
                for d in _DEMONSTRATIVES:
                    if name.startswith(d) and len(name) > len(d):
                        name = name[len(d):]
                        break
                if name == "因子":
                    continue  # 纯代词式指代（"该因子"），非具体因子名，不判
                core = name[:-2] if name.endswith("因子") else name
                if not core:
                    continue
                if core not in material_text and name not in factor_allowlist and core not in " ".join(factor_allowlist):
                    return GuardVerdict(False, "evidence",
                                        f"凭空因子: '{name}' 不在原料（core='{core}' 无原料背书）")
        return GuardVerdict(True)

    # -- 闸3 --
    def _check_trace(self, sentences) -> GuardVerdict:
        for s in sentences:
            if s.source not in SOURCE_WHITELIST:
                return GuardVerdict(False, "trace",
                                    f"source 缺失或不在白名单: {s.source!r} ∈ {SOURCE_WHITELIST}")
        return GuardVerdict(True)

    # -- 主入口 --
    def check(self, sentences) -> GuardVerdict:
        """三闸串联，任一不过即拒。sentences: list[AdvisorSentence]"""
        sentences = list(sentences)
        v = self._check_trace(sentences)      # 先验可追溯（来源不明的一切免谈）
        if not v.ok:
            return v
        v = self._check_conclusion(sentences)
        if not v.ok:
            return v
        v = self._check_evidence(sentences)
        if not v.ok:
            return v
        return GuardVerdict(True)

    # -- 降级路径（诚实水位） --
    def degrade(self, sentences) -> DegradedOutput:
        """守卫拒绝时的唯一合法出口：原始 reasoning 原文 + 术语小注。
        不静默、不编造、不回退排名话术。免责句由渲染层统一加（不在此处）。"""
        v = self.check(sentences)
        note = ""  # 术语小注由 rewrite 层按词典生成后传入；守卫层保持纯守卫
        if v.ok:
            text = "\n".join(s.text for s in sentences)
            return DegradedOutput(degraded=False, text=text)
        return DegradedOutput(degraded=True, text=self.arg.reasoning + note)

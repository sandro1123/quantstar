# -*- coding: utf-8 -*-
"""模块1b 二次加工引擎 — 术语翻译 + 规则拼装 + LLM 改写（守卫在闸）

架构（统筹批复 A 案 = LLM 改写 + 确定性守卫）：
  1. translate_terms: 术语词典确定性翻译（内部分析语言 → 用户语言）
  2. 规则腿: 翻译后的 reasoning 逐句切分 → AdvisorSentence（source=reasoning）
  3. LLM 腿（隔离单函数 _llm_rewrite）: 市场化改写，产出仍过 FidelityGuard 三闸
  4. 守卫拒 → 降级 = 原始 reasoning 原文（诚实水位），不静默不编造不回退排名话术
LLM 腿隔离：统筹要改判纯规则（B 案）只动 _llm_rewrite 一处。

契约条款3：1b 的 LLM 改写走报告层 _llm_helper 叙述门（纯文本改写），
不调 agent_layer 重算（消费已产出论证，非触发重跑）。
"""
from .fidelity import FidelityGuard
from .guard_trace import record_guard_rejection, PATH_RAW, PATH_RULE_FALLBACK
from .models import AdvisorSentence, AdvisorResult, RecommendationArgument

from common.logging import get_logger

logger = get_logger("report.advisor.rewrite")

# 术语词典：内部分析语言 → 用户语言（追加式，锚死词典唯一来源）
_TERM_MAP = {
    "IC衰减": "预测力下降",
    "IC回升": "预测力回升",
    "ICIR": "预测稳定性",
    "WFO": "历史推演检验",
    "多空分歧": "多空观点冲突",
    "放量突破": "成交放大突破",
    "缩量回调": "成交萎缩回落",
    "左侧": "提前布局",
    "右侧": "跟随趋势",
    "筹码松动": "持股人抛售增多",
    "套牢盘": "高位被套的持仓",
}

# 无原料时的显式水位（绝不回退排名话术充数）
PENDING_LINE = "理由生成中（论证尚在积累，本条暂不给出推荐理由）"

# 降级时随原文附的术语小注（用户可能遇到的内部分析术语 → 白话）
RULE_NOTE = "（注：以下为系统原始分析表述，含部分专业术语）"


def translate_terms(text: str) -> str:
    """确定性术语翻译。幂等：已翻译文本不二次变化。"""
    out = text
    for k, v in _TERM_MAP.items():
        out = out.replace(k, v)
    return out


def _split_sentences(reasoning: str) -> list:
    """reasoning 切句（中文句号/分号/换行），非空保留"""
    import re
    parts = re.split(r"[。；;\n]+", reasoning or "")
    return [p.strip() for p in parts if p.strip()]


def _llm_rewrite(arg: RecommendationArgument, llm) -> list | None:
    """LLM 腿（隔离单函数）。llm: 有 .call(prompt, **kw) 的门内叙述柜台。
    返回 AdvisorSentence 列表或 None（失败/超时/空回）。失败由调用方走规则腿。"""
    if llm is None:
        return None
    prompt = (
        "你是投资顾问编辑。把下面这段量化分析改写成普通投资者能懂的语言："
        "1.把专业术语翻译成白话；2.让表达有引导力但绝不改变结论方向和力度；"
        "3.绝不添加原料中没有的事实、数字、因子或战绩；4.保持原意，逐句对应。\n"
        f"结论方向: {arg.verdict}；置信度: {arg.confidence:.0%}\n"
        f"原始分析: {arg.reasoning}"
    )
    try:
        resp = llm.call(prompt, temperature=0.3, max_tokens=2000, call_point="advisor_rewrite")
    except Exception:
        return None
    if not resp or not str(resp).strip():
        return None
    sentences = [
        AdvisorSentence(text=p.strip(), source="reasoning")
        for p in _split_sentences(str(resp))
    ]
    return sentences or None


def _rule_rewrite(arg: RecommendationArgument) -> list:
    """规则腿：确定性翻译后逐句切分（零幻觉，表达朴素但完全忠实）"""
    translated = translate_terms(arg.reasoning)
    return [
        AdvisorSentence(text=p, source="reasoning")
        for p in _split_sentences(translated)
    ]


def rewrite_to_advisor(arg: RecommendationArgument, llm=None, trace_db=None, date: str = "") -> AdvisorResult:
    """主入口：LLM 腿优先过守卫；LLM 产出被守卫拒 → 规则腿 + degraded=True
    （诚实标注「LLM 改写未通过忠实性守卫，展示的是确定性翻译版」）；
    规则腿也被拒 → 降级原文。规则腿产出全过守卫且无 LLM → 正常产出。
    trace_db: 传入 sqlite 连接时，守卫拒绝自动落痕 advisor_guard_trace（审计取证面，
    件① 09-21）；留痕失败不阻塞主流程（warn 后继续——留痕是取证面，不是推荐主链）。"""
    if arg is None:
        return AdvisorResult(sentences=[], degraded=False, text=PENDING_LINE)

    def _trace(gate_verdict, path, llm_output=""):
        try:
            record_guard_rejection(trace_db, date=date, code=arg.code,
                                   guard_verdict=gate_verdict, degrade_path=path,
                                   llm_output=llm_output,
                                   rationale_id=getattr(arg, "rationale_id", "") or "",
                                   trace_id=getattr(arg, "trace_id", "") or "")
        except Exception as e:
            logger.warning(f"guard_trace 留痕失败(不阻塞主链): {e}")

    guard = FidelityGuard(arg)
    llm_output_text = ""
    llm_sentences = _llm_rewrite(arg, llm)
    if llm_sentences:
        llm_output_text = "\n".join(s.text for s in llm_sentences)
        v = guard.check(llm_sentences)
        if v.ok:
            return AdvisorResult(sentences=llm_sentences, degraded=False,
                                 text=llm_output_text)
        _trace(v, PATH_RULE_FALLBACK, llm_output=llm_output_text)

    rule_sentences = _rule_rewrite(arg)
    if rule_sentences:
        v = guard.check(rule_sentences)
        if v.ok:
            llm_rejected = llm_sentences is not None
            return AdvisorResult(sentences=rule_sentences, degraded=llm_rejected,
                                 text=("\n".join(s.text for s in rule_sentences)
                                       + (RULE_NOTE if llm_rejected else "")))
        _trace(v, PATH_RAW, llm_output=llm_output_text)

    # 两条腿都被守卫拒（规则腿理论上罕见但必须显式降级，不静默）
    v = guard.check(rule_sentences)
    _trace(v, PATH_RAW, llm_output=llm_output_text)
    return AdvisorResult(sentences=[], degraded=True,
                         text=f"{RULE_NOTE}\n{arg.reasoning}")

# -*- coding: utf-8 -*-
"""入池理由回填 — 甲案第二段 + 三道前置闸（统筹批复 sm-eb9088b2865e4814 / 裁定甲 09-25）

接线设计（冻结）：
  调用点 = plan_manager.receive_plan（入池主链内，stock_pool.add 之前）
  降级   = 零新增 LLM（只读已落库 rationale + 确定性守卫加工）+ 任何异常不阻塞入池
  与 W1 分工 = 他落库（agent 层 _run_debate 末尾 record_rationale）我回填（执行层）

三裁点：
  A 补辩论暂不做 → 真理由覆盖率天花板 = 当日辩论票数（≤ deep_top_n，**默认 10**，
    用户 09-25 裁 5→10 治「理由全是生成中」）。这是特性非缺陷。
  B rationale_id 列不加 → trace JOIN 已有 guard_trace+rationale 两锚，零 schema 变更。
  C 覆盖率天花板写进 docstring（本文件即载体）。

■ 三道前置闸（09-25 实证前预警补，统筹裁定甲）
  病案：recommendation_rationale 全表 0 行（1a 落库腿在 main 但从没产出）→ enrich
  永远走 agent_memory 回落，而那里存的是 agent **自己发起的**分析（news/复盘/盘中），
  不是「为这次推荐做的辩论」→ 拿 A 场景判断解释 B 场景动作，语义错配必然。
  09-24 三票模拟实测：001319 回填「未调LLM」规则腿产物（比排名话术更糟——排名话术
  至少诚实）；688668 内部标记泄漏且 verdict=avoid 却配 buy 动作；603322 理由真但
  verdict=观望 同样配不上 buy。

  **忠实性守卫守的是「加工不改原料结论」，本卡补的是「原料适不适合这个用途」。**

  闸A 动作一致：入池 action=buy 而原料 verdict ∈ {不推荐,观望,警惕,...} → 拒
    （「推荐买入…理由：建议观望」比没理由更糟，直接违背 REC 卡「有理」原则）
  闸B 原料质量：reasoning 是规则腿产物/未调 LLM → 不算真理由 → 拒
  闸C 标记清洗：内部标记 [..]/【..】/换行 → 清洗；清洗后空 → 拒

  三闸全 restrictive：**拒 = 保持原 added_reason**（走下游 _pick_reason 诚实占位闸），
  失败模式 = 现状，不会比不加闸更差（统筹论证：甲严格优于乙）。

铁律#12 硬边界：enrich_reason 绝不抛——任何异常降级成「保持原 added_reason」，
绝不阻断入池主链。
"""
import re

from common.logging import get_logger

logger = get_logger("execution.plan_manager.enrich")

# 诚实占位（与 report_reason.NO_REASON_TEXT 同语义，执行层独立定义防跨层 import 反向）
PENDING_NOTE = "（个股依据生成中）"

# 闸A: AG-03 词表里与「买入动作」相容的判断（其余一律视为不相容）
_BUY_COMPATIBLE_VERDICTS = {"推荐", "买入", "buy"}

# 闸B: 规则腿/未调 LLM 的产物标记（这些不是「理由」，是内部状态）
_RULE_LEG_MARKS = ("未调LLM", "未调llm", "规则模式", "[规则腿]", "规则腿",
                   "rule_mode", "rules_only")

# 闸C: 内部标记（方括号注/书名号判定/换行）——展示给用户前须清洗
_BRACKET_RE = re.compile(r"\[[^\]]*\]|【[^】]*】")
_WS_RE = re.compile(r"\s+")


def passes_action_gate(action: str, verdict) -> bool:
    """闸A：入池动作与原料判断是否相容。

    只管 buy（其余动作不在本闸管辖，放行交后续闸）。
    判断缺失 → 拒（不拿无判断的料当理由）。
    """
    if (action or "").strip().lower() != "buy":
        return True
    v = (verdict or "").strip()
    if not v:
        return False
    return v in _BUY_COMPATIBLE_VERDICTS


def is_rule_leg_artifact(text) -> bool:
    """闸B：是否规则腿产物（不算真理由）。空也算（无料）。"""
    t = (text or "").strip()
    if not t:
        return True
    return any(m in t for m in _RULE_LEG_MARKS)


def sanitize_internal_marks(text) -> str:
    """闸C：清洗内部标记与换行，返回可展示文本（可能为空串→调用方据此拒）。"""
    t = _BRACKET_RE.sub(" ", text or "")
    t = _WS_RE.sub(" ", t)
    return t.strip()


def _load_adviser_conn(pm):
    """入池侧的数据库连接（PlanManager.db）。取不到返回 None。"""
    try:
        db = getattr(pm, "db", None)
        return db.get_connection() if db else None
    except Exception:
        return None


def enrich_reason(pm, code: str, date: str, original_reason: str,
                  conn=None, action: str = "") -> str:
    """单票理由回填：原料过三闸 + 忠实性守卫后才替换；任一不过 → 保持原样。

    契约：
    - 零新增 LLM：load_argument 只读已落库论证，rewrite_to_advisor 走规则腿（确定性）。
    - 绝不抛：任何异常返回 original_reason（入池主链优先，铁律#12）。
    - 三闸全 restrictive：拒 = 保持原样（下游 _pick_reason 出诚实占位），不写脏理由。
    - action 缺省（既有调用点不传）→ 闸A 放行，交闸B/C 与守卫把关（向后兼容）。
    """
    try:
        if not code or not date:
            return original_reason
        conn = conn or _load_adviser_conn(pm)
        if conn is None:
            return original_reason

        from report_layer.advisor import load_argument, rewrite_to_advisor
        arg = load_argument(code, date, conn)
        if arg is None:
            return original_reason            # 无料 → 原样（诚实水位在下游闸）

        # ---- 闸A: 动作一致（原料判断须与入池动作相容）----
        # 单一定义点: 判定逻辑只在 passes_action_gate 里（白名单同时含 AG-03
        # 中文词表与 load_argument 映射后的内部语义域, 两种入参都判得准）。
        # 曾内联过一份等价判定 → 同一判定两处实现迟早漂移, 已收口。
        if not passes_action_gate(action, arg.verdict):
            logger.info(f"enrich: {code} 闸A 拒（入池 buy 但原料判断={arg.verdict}）"
                        f"—— 不写自相矛盾的理由，保持原样")
            return original_reason

        # ---- 闸B: 原料质量（规则腿产物不算真理由）----
        if is_rule_leg_artifact(arg.reasoning):
            logger.info(f"enrich: {code} 闸B 拒（规则腿/未调LLM 产物非真理由）")
            return original_reason

        result = rewrite_to_advisor(arg, llm=None)   # 零 LLM：规则腿+忠实性守卫
        if result.degraded or not result.sentences:
            logger.warning(f"enrich: {code} 守卫拒/无产出，保持原样 "
                           f"(degraded={result.degraded})")
            return original_reason

        # ---- 闸C: 内部标记清洗 ----
        text = sanitize_internal_marks(result.text)
        if not text:
            logger.info(f"enrich: {code} 闸C 拒（清洗后无可展示内容）")
            return original_reason
        if is_rule_leg_artifact(text):            # 清洗后仍是规则腿标记
            logger.info(f"enrich: {code} 闸C 拒（清洗后仍含规则腿标记）")
            return original_reason
        return text
    except Exception as e:                     # noqa: BLE001 — 铁律#12：绝不炸入池
        logger.warning(f"enrich: {code} 理由回填失败(不阻塞入池): {e}")
        return original_reason

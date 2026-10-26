# -*- coding: utf-8 -*-
"""plan_manager.receive_plan 接线补丁（幂等）。

在循环开头 reason 取值后加 enrich（入池主链内，绝不抛，降级=原样）。
"""
import io

P = "src/execution_layer/core/plan_manager.py"
src = io.open(P, encoding="utf-8", newline="").read()
orig = src

# 1. import（模块顶部，正交于既有 import）
if "from execution_layer.core.enrich_reason import enrich_reason" not in src:
    src = src.replace(
        "from execution_layer.core.human_in_loop import HumanInLoop",
        "from execution_layer.core.human_in_loop import HumanInLoop\n"
        "from execution_layer.core.enrich_reason import enrich_reason",
        1)

# 2. 循环内 reason 取值后接 enrich（守卫接线点）
if "enrich_reason(" not in src.split("def receive_plan")[1].split("def ")[0]:
    src = src.replace(
        """            confidence = rec.get("confidence", 0.5)
            reason = rec.get("reason", "")""",
        """            confidence = rec.get("confidence", 0.5)
            reason = rec.get("reason", "")
            # 甲案第二段 (2026-09-25): 入池理由回填 — rationale 有料 → 守卫加工后
            # 真理由; 无料/守卫拒/异常 → 原样 (绝不抛, 铁律#12: 入池主链优先)。
            # 零新增 LLM (只读 agent 层已落库 rationale + 确定性守卫)。
            # 真理由覆盖率天花板 = 当日辩论票数 (≤ deep_top_n), 特性非缺陷。
            reason = enrich_reason(self, code, plan.get("date", bj_date()), reason)""",
        1)

assert src != orig, "补丁未命中（锚点漂移，勿上传）"
io.open(P, "w", encoding="utf-8", newline="").write(src)
print("patched OK; CRLF=", src.count("\r"))

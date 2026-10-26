"""AG-03: Agent 输出契约 (OutputSpec) — verdict 统一词表 + 每 task_type 输出契约

统一 verdict 枚举: 推荐 / 不推荐 / 观望 / 警惕
- 推荐: 正向结论 (个股值得买入 / 市场看多 / 审核通过 / 退出合理)
- 不推荐: 负向结论 (回避 / 市场看空 / 审核拒绝 / 退出存疑)
- 观望: 中性/信号不足/降级
- 警惕: 风险提示或依据缺失, 参考信息不构成方向阻断

历史词表归一化: approve→推荐, reject→不推荐, 关注/买入→推荐,
卖出→不推荐, neutral/hold/watch→观望。
消费方 (执行层/策略层/报告层) 只允许消费统一枚举。
"""

ALLOWED_VERDICTS = ("推荐", "不推荐", "观望", "警惕")

# 历史词表 → 统一词表 (仅用于归一化存量输出, 新代码禁止产生旧词)
_LEGACY_MAP = {
    "approve": "推荐",
    "reject": "不推荐",
    "关注": "推荐",
    "买入": "推荐",
    "卖出": "不推荐",
    "neutral": "观望",
    "hold": "观望",
    "watch": "观望",
}

# 每 task_type 的输出契约: 必填字段 + verdict 语义
OUTPUT_SPECS = {
    "stock_analysis": {
        "required": ("verdict", "confidence", "reasoning"),
        "verdict_semantics": "推荐=值得买入, 不推荐=回避, 观望=信号不足, 警惕=风险突出",
    },
    "market_analysis": {
        "required": ("verdict", "confidence", "reasoning"),
        "verdict_semantics": "推荐=看多, 不推荐=看空, 观望=震荡无方向",
    },
    "entry_check": {
        "required": ("verdict", "confidence", "reasoning"),
        "verdict_semantics": "推荐=审核通过, 不推荐=审核拒绝, 警惕=通过但有风险标记",
    },
    "exit_review": {
        "required": ("verdict", "confidence", "reasoning"),
        "verdict_semantics": "推荐=退出合理(参考放行), 不推荐=退出存疑(参考), 警惕=退出依据缺失",
    },
}


def normalize_verdict(verdict) -> str:
    """把任意历史 verdict 归一化到统一枚举; 未知值诚实落 观望 (不伪造方向)"""
    if not isinstance(verdict, str):
        return "观望"
    v = verdict.strip()
    if v in ALLOWED_VERDICTS:
        return v
    return _LEGACY_MAP.get(v, "观望")


def validate_output_spec(task: str, payload: dict) -> list[str]:
    """按 task 契约校验输出, 返回警告列表 (空=合规); 只告警不阻断"""
    warnings = []
    spec = OUTPUT_SPECS.get(task)
    if spec is None:
        return warnings
    for field_name in spec["required"]:
        if payload.get(field_name) in (None, ""):
            warnings.append(f"OutputSpec[{task}]: 缺字段 {field_name}")
    verdict = payload.get("verdict")
    if verdict not in (None, "") and verdict not in ALLOWED_VERDICTS:
        warnings.append(f"OutputSpec[{task}]: verdict '{verdict}' 不在统一枚举 {ALLOWED_VERDICTS}")
    conf = payload.get("confidence")
    if isinstance(conf, (int, float)) and not (0 <= conf <= 1):
        warnings.append(f"OutputSpec[{task}]: confidence {conf} 越界 [0,1]")
    return warnings

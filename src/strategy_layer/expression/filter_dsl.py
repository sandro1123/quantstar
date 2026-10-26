"""声明式筛选 DSL — 在表达式 DSL 基础上扩展筛选能力"""

from dataclasses import dataclass, field

from strategy_layer.expression.parser import evaluate, parse, tokenize


@dataclass
class FilterCondition:
    expression: str
    description: str = ""
    enabled: bool = True


@dataclass
class FilterResult:
    passed: list[str]
    failed: list[str]
    reasons: dict[str, str] = field(default_factory=dict)


class FilterDSL:
    """声明式筛选 DSL"""

    def __init__(self):
        self.conditions: list[FilterCondition] = []

    def add_condition(self, condition: FilterCondition):
        self.conditions.append(condition)

    def add_condition_from_str(self, expr: str, desc: str = ""):
        self.conditions.append(FilterCondition(expression=expr, description=desc))

    def apply(self, stock_contexts: dict[str, dict[str, float]]) -> FilterResult:
        passed = []
        failed = []
        reasons = {}

        for code, ctx in stock_contexts.items():
            code_passed = True
            for cond in self.conditions:
                if not cond.enabled:
                    continue
                try:
                    ast = parse(tokenize(cond.expression))
                    result = evaluate(ast, ctx)
                    if not result:
                        code_passed = False
                        reasons[code] = reasons.get(code, "") + f"{cond.description}; "
                        break
                except Exception:
                    code_passed = False
                    reasons[code] = reasons.get(code, "") + f"error evaluating '{cond.expression}'; "
                    break

            if code_passed:
                passed.append(code)
            else:
                failed.append(code)

        return FilterResult(passed=passed, failed=failed, reasons=reasons)

    @classmethod
    def default_stock_filters(cls) -> 'FilterDSL':
        """创建默认的股票筛选条件"""
        dsl = cls()
        dsl.add_condition_from_str("composite_score > 0", "综合评分 > 0")
        return dsl

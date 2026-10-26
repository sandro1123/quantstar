"""配置校验 — 启动时检查必填项 + 类型 + 约束"""

import re
from dataclasses import dataclass, field

from config_layer.registry import ConfigRegistry


@dataclass
class ValidationResult:
    is_valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _check_constraints(entry, key: str) -> list[str]:
    warnings = []
    if entry.constraints:
        val = entry.value
        c = entry.constraints
        if "min" in c and isinstance(val, (int, float)) and val < c["min"]:
            warnings.append(f"{key}: {val} < min {c['min']}")
        if "max" in c and isinstance(val, (int, float)) and val > c["max"]:
            warnings.append(f"{key}: {val} > max {c['max']}")
        if "options" in c and val not in c["options"]:
            warnings.append(f"{key}: {val} not in {c['options']}")
        if "min_length" in c and isinstance(val, str) and len(val) < c["min_length"]:
            warnings.append(f"{key}: length {len(val)} < min_length {c['min_length']}")
        if "regex" in c and isinstance(val, str):
            if not re.match(c["regex"], val):
                warnings.append(f"{key}: does not match regex {c['regex']}")
    return warnings


class ConfigValidator:
    def validate(self, registry: ConfigRegistry) -> ValidationResult:
        result = ValidationResult()
        for key, entry in registry._entries.items():
            val = entry.value
            if val is None:
                result.warnings.append(f"{key}: 未设置，使用默认值 None")
                continue
            try:
                casted = entry._cast(val)
                if casted is not None and not isinstance(casted, entry.type_):
                    result.warnings.append(
                        f"{key}: 类型应为 {entry.type_.__name__}，实际为 {type(casted).__name__}"
                    )
            except (ValueError, TypeError):
                result.errors.append(f"{key}: 无法转换为 {entry.type_.__name__}")
                result.is_valid = False
            result.warnings.extend(_check_constraints(entry, key))
        # CF-07: 资金基准唯一性断言（D-26）
        if "backtest.initial_capital" in registry._entries:
            result.warnings.append(
                "backtest.initial_capital 已废弃（D-26 统一资金基准），请使用 execution.initial_capital"
            )
        return result

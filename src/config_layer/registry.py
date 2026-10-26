"""配置注册表 — 自注册 + 类型安全 + 秘密脱敏"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ConfigEntry:
    key: str
    type_: type
    default: Any = None
    description: str = ""
    secret: bool = False
    env_var: str | None = None
    constraints: dict | None = None
    _value: Any = None

    @property
    def value(self) -> Any:
        if self._value is not None:
            return self._cast(self._value)
        if self.env_var:
            env_val = os.environ.get(self.env_var)
            if env_val is not None:
                return self._cast(env_val)
        return self._cast(self.default) if self.default is not None else None

    @value.setter
    def value(self, v: Any):
        self._value = v

    def _cast(self, v: Any) -> Any:
        try:
            if self.type_ is bool and isinstance(v, str):

                return v.lower() in ("true", "1", "yes")
            if self.type_ is int:

                return int(v)
            if self.type_ is float:

                return float(v)
            if self.type_ is str:

                return str(v)
        except (ValueError, TypeError):
            pass
        return v


class ConfigRegistry:
    def __init__(self):
        self._entries: dict[str, ConfigEntry] = {}
        self._change_callbacks: dict[str, list[Callable]] = {}

    def register(
        self,
        key: str,
        type_: type,
        default: Any = None,
        description: str = "",
        secret: bool = False,
        env_var: str | None = None,
        constraints: str | None = None,
    ):
        self._entries[key] = ConfigEntry(
            key=key, type_=type_, default=default,
            description=description, secret=secret,
            env_var=env_var, constraints=constraints,
        )

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            return default
        val = entry.value
        if val is None:
            return default
        return val

    def set(self, key: str, value: Any):
        entry = self._entries.get(key)
        if entry:
            entry.value = value
            cbs = self._change_callbacks.get(key, [])
            for cb in cbs:
                cb(value)
        else:
            # 自动注册未预定义的键
            self._entries[key] = ConfigEntry(
                key=key, type_=type(value), default=None,
                description="auto-registered",
            )
            self._entries[key].value = value

    def to_dict(self, show_secrets: bool = False) -> dict:
        result = {}
        for key, entry in self._entries.items():
            if entry.secret and not show_secrets:
                result[key] = "******"
            else:
                result[key] = entry.value
        return result

    def on_change(self, key: str, callback: Callable):
        if key not in self._change_callbacks:
            self._change_callbacks[key] = []
        self._change_callbacks[key].append(callback)

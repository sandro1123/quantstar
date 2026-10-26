"""源注册表 — Data Layer 内部管理所有数据源 (DL-10: 统一注册表, 原双注册表已合并为单一文件)"""

import logging

from data_layer.sources.protocol import DataSource

logger = logging.getLogger("data.source_registry")


class SourceRegistry:
    """源注册表 — 管理所有数据源

    支持全局优先级 + 按数据类型覆盖优先级 (DL-02):
    register(source, priority=50, type_priorities={"kline": 1, ...})
    get_sources_for 排序时, type_priorities[data_type] 优先于全局 priority。
    """

    def __init__(self):
        self._sources: dict[str, DataSource] = {}
        self._priority: dict[str, int] = {}
        self._type_priority: dict[str, dict[str, int]] = {}

    def register(self, source: DataSource, priority: int = 50,
                 type_priorities: dict[str, int] | None = None):
        """注册一个源

        Args:
            source: 数据源适配器
            priority: 全局优先级 (越小越优先)
            type_priorities: 按数据类型覆盖优先级, 如 {"limit_up": 1, "kline": 2}
        """
        self._sources[source.name] = source
        self._priority[source.name] = priority
        if type_priorities:
            self._type_priority[source.name] = type_priorities
        logger.info(f"Registered source: {source.name} (priority={priority}"
                    f"{', type_priorities=' + str(type_priorities) if type_priorities else ''})")

    def unregister(self, name: str):
        """注销一个源"""
        if name in self._sources:
            del self._sources[name]
            del self._priority[name]
            self._type_priority.pop(name, None)
            logger.info(f"Unregistered source: {name}")

    def get_sources_for(self, data_type: str) -> list[DataSource]:
        """获取能提供某数据类型的所有可用源（按优先级排序）

        排序规则: type_priorities[data_type] 优先, 否则用全局 priority。
        """
        sources = []
        for s in self._sources.values():
            if data_type in s.capabilities and s.is_available():
                tp = self._type_priority.get(s.name, {})
                prio = tp.get(data_type, self._priority.get(s.name, 50))
                sources.append((s, prio))
        sources.sort(key=lambda x: x[1])
        return [s for s, _ in sources]

    def get_best_source(self, data_type: str) -> DataSource | None:
        """获取某数据类型的最佳可用源"""
        sources = self.get_sources_for(data_type)
        return sources[0] if sources else None

    def get_all_sources(self) -> list[DataSource]:
        """获取所有已注册的源"""
        return list(self._sources.values())

    def get_available_sources(self) -> list[DataSource]:
        """获取所有可用的源"""
        return [s for s in self._sources.values() if s.is_available()]

    def is_registered(self, name: str) -> bool:
        """检查源是否已注册"""
        return name in self._sources

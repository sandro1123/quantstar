"""多源融合策略"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("data.fusion")


@dataclass
class DataDifference:
    """数据差异"""
    field_name: str
    source1: str
    value1: Any
    source2: str
    value2: Any
    diff_ratio: float


@dataclass
class ValidationReport:
    """验证报告"""
    data_type: str
    differences: list[DataDifference] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 卡②: 源级错误留痕 {源名: 错误摘要} — 供管线把 failed 的 reason 指向病因
    # (统筹裁定 sm-13caf48691a54065: 失败信息必须指向病因, 不是指向症状)。
    source_errors: dict = field(default_factory=dict)

    def add_difference(self, field_name: str, source1: str, value1: Any,
                       source2: str, value2: Any, threshold: float = 0.01):
        """记录差异"""
        if value1 == 0 and value2 == 0:
            return

        max_val = max(abs(value1), abs(value2), 1)
        diff = abs(value1 - value2) / max_val

        if diff > threshold:
            self.differences.append(DataDifference(
                field_name=field_name,
                source1=source1, value1=value1,
                source2=source2, value2=value2,
                diff_ratio=diff
            ))
            self.warnings.append(
                f"数据差异告警: {field_name} 在 {source1} 和 {source2} 之间差异 {diff:.2%}"
            )

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


class FusionStrategy:
    """多源融合策略"""

    def __init__(self, registry):
        self.registry = registry

    def fuse(self, data_type: str, params: dict,
             threshold: float = 0.01,
             key_field: str | None = None) -> tuple[Any, ValidationReport | None]:
        """
        融合多源数据:
        1. 获取所有可用源
        2. 查询多个源
        3. 对比差异 (list[dict] 可指定 key_field 按业务键对齐, 如 "code")
        4. 返回最佳数据 + 验证报告
        """
        sources = self.registry.get_sources_for(data_type)

        if not sources:
            logger.warning(f"No source available for {data_type}")
            return None, None

        if len(sources) == 1:
            try:
                data = sources[0].get_data(data_type, params)
                return data, None
            except Exception as e:
                # 卡②: 单源路径同样留痕病因 (旧实现只 log 后返回 (None, None),
                # 上游无法区分"源报错"与"源干净返空" → failed 退化为 partial)。
                logger.error(f"Source {sources[0].name} failed: {e}")
                return None, ValidationReport(
                    data_type=data_type,
                    source_errors={sources[0].name: f"{type(e).__name__}: {e}"})

        results = {}
        source_errors: dict[str, str] = {}
        for source in sources:
            try:
                data = source.get_data(data_type, params)
                if data is not None:
                    results[source.name] = data
            except Exception as e:
                # 卡②: 源级错误必须留痕 (不只 log) — 上游据此把 failed 的
                # reason 指向病因而非症状 (旧文案 "所有源均无数据" 让人猜源)。
                source_errors[source.name] = f"{type(e).__name__}: {e}"
                logger.warning(f"Source {source.name} failed: {e}")

        if not results:
            if source_errors:
                # 有源但全失败 → 带病因的报告返回 (调用方据 source_errors 判 failed)
                return None, ValidationReport(data_type=data_type,
                                              source_errors=source_errors)
            return None, None

        if len(results) == 1:
            source_name = list(results.keys())[0]
            return results[source_name], (ValidationReport(data_type=data_type,
                                                          source_errors=source_errors)
                                          if source_errors else None)

        validation_report = self._validate(data_type, results, threshold, key_field)
        if source_errors:
            validation_report.source_errors = source_errors

        # 最佳源优先; 最佳源无结果时回退到第一个成功的源 (不丢数据)
        # 卡① (2026-09-15 lhb 断采定谳 A): "无结果" 必须包含**空容器** —
        # 只判 `is None` 时主源返回 [] 会让空容器胜出, 备源真数据被丢弃。
        # 09-14 实证: zzshare 腿实有 72 行, 因 fuyao 腿返 [] 而被弃,
        # 当日 lhb 误报 "所有源均无数据"。空容器与 None 同等对待。
        best_data = results.get(sources[0].name)
        if not best_data:
            for source in sources[1:]:
                if results.get(source.name):
                    best_data = results[source.name]
                    break

        return best_data, validation_report

    def _validate(self, data_type: str, results: dict,
                  threshold: float, key_field: str | None = None) -> ValidationReport:
        """对比多个源的数据差异

        list[dict] 对齐方式:
        - 指定 key_field 且两源数据均含该键 → 按业务键对齐 (两源排序不同不误报)
        - 否则按 index 对齐 (兼容无业务键的数据)
        """
        report = ValidationReport(data_type=data_type)

        source_names = list(results.keys())

        for i in range(len(source_names)):
            for j in range(i + 1, len(source_names)):
                source1 = source_names[i]
                source2 = source_names[j]
                data1 = results[source1]
                data2 = results[source2]

                if isinstance(data1, dict) and isinstance(data2, dict):
                    common_keys = set(data1.keys()) & set(data2.keys())
                    for key in common_keys:
                        val1 = data1[key]
                        val2 = data2[key]
                        if isinstance(val1, (int, float)) and isinstance(val2, (int, float)):
                            report.add_difference(key, source1, val1, source2, val2, threshold)
                elif isinstance(data1, list) and isinstance(data2, list):
                    pairs = self._align_lists(data1, data2, key_field)
                    for label, item1, item2 in pairs:
                        if isinstance(item1, dict) and isinstance(item2, dict):
                            common_keys = set(item1.keys()) & set(item2.keys())
                            for key in common_keys:
                                v1, v2 = item1[key], item2[key]
                                if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                                    report.add_difference(f"{label}.{key}", source1, v1, source2, v2, threshold)

        return report

    @staticmethod
    def _align_lists(data1: list, data2: list,
                     key_field: str | None) -> list[tuple[str, Any, Any]]:
        """对齐两个 list[dict]: 有 key_field 按业务键, 否则按 index。

        返回 [(标签, item1, item2), ...], 标签用于差异定位 (如 "code=600519" 或 "[3]")。
        """
        if key_field:
            keyed1 = {item.get(key_field): item for item in data1
                      if isinstance(item, dict) and item.get(key_field) is not None}
            keyed2 = {item.get(key_field): item for item in data2
                      if isinstance(item, dict) and item.get(key_field) is not None}
            # 两源均含业务键才走键对齐, 否则退回 index 对齐
            if keyed1 and keyed2 and len(keyed1) == len(data1) and len(keyed2) == len(data2):
                common = set(keyed1.keys()) & set(keyed2.keys())
                return [(f"{key_field}={k}", keyed1[k], keyed2[k]) for k in sorted(common, key=str)]
        min_len = min(len(data1), len(data2))
        return [(f"[{idx}]", data1[idx], data2[idx]) for idx in range(min_len)]

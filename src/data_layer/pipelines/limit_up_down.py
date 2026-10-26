"""LimitUpDownPipeline — 涨停/跌停池数据管道

通过 SourceRegistry 获取涨停/跌停池数据。DL-01 后优先走 FusionStrategy.fuse()
(多源查询 + 按 code 对齐交叉验证, 差异超阈值产生告警但不阻塞, 符合 D198);
无 fusion 时退回逐源 fallback 循环。
"""

from __future__ import annotations

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.limit_up_down")


class LimitUpDownPipeline(BasePipeline):
    name = "stock_limit_up_down"
    requires = []
    produces = ["stock_limit_up_down"]

    def __init__(self, db=None, source_registry=None, fusion=None):
        self.db = db
        self.source_registry = source_registry
        self.fusion = fusion

    def _fetch_pool(self, data_type: str, date: str) -> tuple[list[dict], list[str]]:
        """拉涨停/跌停池, 返回 (rows, validation_warnings)。

        有 fusion 时走 fuse() (多源交叉验证); 否则逐源尝试直到拿到数据。
        """
        if self.fusion is not None:
            rows, report = self.fusion.fuse(data_type, {"date": date}, key_field="code")
            warnings = list(report.warnings) if report is not None else []
            if report is not None and report.has_warnings:
                logger.warning(f"limit_up_down {data_type} 多源差异: {len(report.warnings)} 条告警")
            if rows:
                logger.info(f"limit_up_down {data_type} via fusion: {len(rows)} rows")
            return rows or [], warnings

        if not self.source_registry:
            return [], []
        sources = self.source_registry.get_sources_for(data_type)
        intended = sources[0].name if sources else ""
        for source in sources:
            try:
                rows = source.get_data(data_type, {"date": date})
                if rows:
                    logger.info(f"limit_up_down {data_type} from {source.name}: {len(rows)} rows")
                    # DL-26: 非主源拿到数据 → 主源失效留痕 (设计 §15)。
                    # fuyao case: 主源返回 0 行 (falsy) 静默跳到 akshare,
                    # 连续 4 天零告警 (F-23)。此处落 SourceSwitchRecord 使其可观测。
                    if source.name != intended:
                        try:
                            from data_layer.source_switch import record_source_switch
                            record_source_switch(
                                self.db, data_type=data_type,
                                intended_source=intended, actual_source=source.name,
                                reason=f"主源 {intended} 返回 0 行或失败",
                                pipeline=self.name)
                        except Exception:  # noqa: BLE001 — 留痕失败不阻断
                            pass
                    return rows, []
            except Exception as e:
                logger.debug(f"Source {source.name} failed for {data_type}: {e}")
                continue
        return [], []

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        conn = db.get_connection()
        errors: list[str] = []
        warnings: list[str] = []
        total = 0
        source_used = []

        # 口径归一: 源返回 up/down, 历史库存 limit_up/limit_down → 统一为后者
        type_norm = {"up": "limit_up", "down": "limit_down"}

        # 涨停池
        up_rows, up_warnings = self._fetch_pool("limit_up", input.date)
        warnings.extend(up_warnings)
        if up_rows:
            source_used.append(f"limit_up:{up_rows[0].get('_source', 'registry')}")
            for r in up_rows:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO stock_limit_up_down "
                        "(code, date, name, limit_type, price, change_pct, count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            r.get("code", ""),
                            input.date,
                            r.get("name", ""),
                            type_norm.get(r.get("limit_type", "up"), r.get("limit_type", "up")),
                            float(r.get("price", 0) or 0),
                            float(r.get("change_pct", 0) or 0),
                            int(r.get("count", 0) or 0),
                        ),
                    )
                    total += 1
                except Exception as e:
                    errors.append(f"row insert failed: {e}")
            conn.commit()
        else:
            errors.append("no limit_up data from any source")

        # 跌停池
        down_rows, down_warnings = self._fetch_pool("limit_down", input.date)
        warnings.extend(down_warnings)
        if down_rows:
            for r in down_rows:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO stock_limit_up_down "
                        "(code, date, name, limit_type, price, change_pct, count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            r.get("code", ""),
                            input.date,
                            r.get("name", ""),
                            type_norm.get(r.get("limit_type", "down"), r.get("limit_type", "down")),
                            float(r.get("price", 0) or 0),
                            float(r.get("change_pct", 0) or 0),
                            int(r.get("count", 0) or 0),
                        ),
                    )
                    total += 1
                except Exception as e:
                    errors.append(f"row insert failed: {e}")
            conn.commit()
        else:
            # 跌停可能为0(无跌停股), 不算错误
            logger.info("limit_down pool empty (may be no limit-down stocks today)")

        status = "success" if total > 0 else ("failed" if errors else "partial")
        return PipelineResult(
            name=self.name,
            date=input.date,
            status=status,
            records_written=total,
            errors=errors,
            warnings=warnings,
            metadata={"sources": source_used, "up_count": len(up_rows), "down_count": len(down_rows)},
        )

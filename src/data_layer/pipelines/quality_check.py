"""pipelines/quality_check.py — DL-11 数据质量门禁管线

接线 DataQuality (F-02 死代码处置): 每日管线末端跑 report(date),
结果写 quality_checks 表。ERROR 级进运维日报, CRITICAL 级告警 (D98-D99)。

quality_checks 表由本管线建表 (DL-12 复用同一 schema)。
"""

from __future__ import annotations

from common.logging import get_logger
from data_layer.health.quality import DataQuality
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.quality_check")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quality_checks (
    date TEXT NOT NULL,
    table_name TEXT NOT NULL DEFAULT '',
    check_name TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'ERROR',
    result TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
    PRIMARY KEY (date, check_name, table_name)
);
"""


def _classify_level(flag: str) -> str:
    """flag → 告警级别。无数据 (整表缺失) 为 CRITICAL, 其余为 ERROR。"""
    if "无数据" in flag or "表不存在" in flag:
        return "CRITICAL"
    return "ERROR"


def _extract_table(flag: str) -> str:
    """从 flag 文本提取表名 (flag 格式: 'table: 描述')。"""
    if ":" in flag:
        return flag.split(":", 1)[0].strip()
    return ""


class QualityCheckPipeline(BasePipeline):
    """数据质量门禁 — 每日管线末端执行, 结果落 quality_checks 表。"""

    name = "quality_check"
    requires = []
    produces = ["quality_checks"]

    def __init__(self, db=None):
        self.db = db

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        conn = db.get_connection()
        conn.executescript(_SCHEMA)

        quality = DataQuality(db)
        report = quality.report(input.date)

        written = 0
        errors: list[str] = []

        # 每个 flag 写一行 (ERROR/CRITICAL)
        for flag in report.flags:
            level = _classify_level(flag)
            table_name = _extract_table(flag)
            check_name = f"flag_{table_name}" if table_name else "flag_general"
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO quality_checks "
                    "(date, table_name, check_name, level, result, detail) "
                    "VALUES (?, ?, ?, ?, 'fail', ?)",
                    (input.date, table_name, check_name, level, flag),
                )
                written += 1
            except Exception as e:
                errors.append(f"写入 quality_checks 失败: {e}")

        # 无 flag 时写一行 pass 汇总 (保证表每日有记录, 供 DL-12 连续 3 日验收)
        if not report.flags:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO quality_checks "
                    "(date, table_name, check_name, level, result, detail) "
                    "VALUES (?, '', 'all_passed', 'INFO', 'pass', '全部质量检查通过')",
                    (input.date,),
                )
                written += 1
            except Exception as e:
                errors.append(f"写入 quality_checks 失败: {e}")

        conn.commit()

        critical = [f for f in report.flags if _classify_level(f) == "CRITICAL"]
        if critical:
            logger.error(f"quality_check CRITICAL: {critical}")

        status = "success" if not errors else "partial"
        return PipelineResult(
            name=self.name, date=input.date, status=status,
            records_written=written, errors=errors,
            metadata={
                "flags": len(report.flags),
                "critical": len(critical),
                "is_passed": report.is_passed,
            },
        )

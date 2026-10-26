"""Schema 演进管理 — 新字段四阶段处理"""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from common.errors import ErrorSeverity, QuantStarError

logger = logging.getLogger("data.evolution")

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_VALID_FIELD_TYPES = {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"}


@dataclass
class ValidationResult:
    """validate() 返回类型"""
    valid: bool
    reason: str | None = None
    samples: int = 0


@dataclass
class PromotionResult:
    """promote() 返回类型"""
    success: bool
    error: QuantStarError | None = None


@dataclass
class NewField:
    """新字段"""
    table: str
    field_name: str
    field_type: str
    sample_value: Any
    source: str


# AUD-002 警示: 下面 DDL 的时间列默认是 **UTC** (CURRENT_TIMESTAMP /
# datetime('now')), 与全库 BJT 主流 (datetime('now','+8 hours')) 不同。
# 加日期过滤、时间窗查询或渲染给人看之前 **必须先转换**, 否则差 8 小时。
# 禁止只把这里的默认值改成 BJT —— 那会让新行 BJT、存量行 UTC 落进同一列,
# 从「一致的 UTC(安全)」退化成「同列混写(静默出错)」; 要改须连存量一起平移。
# 定谳与三路方案见 docs/audit/OPUS5_DEEP_AUDIT.md 的 AUD-002 段。
class SchemaEvolution:
    """Schema 演进管理"""

    def __init__(self, db):
        self.db = db
        self._init_quarantine_table()

    def _init_quarantine_table(self):
        """初始化隔离区表"""
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_quarantine (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                field_name TEXT NOT NULL,
                field_type TEXT NOT NULL,
                sample_value TEXT,
                source TEXT,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending'
            )
        """)
        conn.commit()

    def detect_new_fields(self, table: str, data: dict, source: str) -> list[NewField]:
        """检测数据中的新字段"""
        existing_columns = self._get_table_columns(table)
        new_fields = []

        for key, value in data.items():
            if key not in existing_columns:
                field_type = self._infer_type(value)
                new_fields.append(NewField(
                    table=table,
                    field_name=key,
                    field_type=field_type,
                    sample_value=value,
                    source=source
                ))
                self._log_detection(table, key, field_type, source)

        return new_fields

    def quarantine(self, field: NewField):
        """将新字段放入隔离区"""
        conn = self.db.get_connection()
        conn.execute(
            "INSERT INTO schema_quarantine (table_name, field_name, field_type, sample_value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (field.table, field.field_name, field.field_type,
             json.dumps(field.sample_value), field.source)
        )
        conn.commit()
        logger.info(f"Quarantined new field: {field.table}.{field.field_name}")

    def validate(self, table: str, field_name: str) -> ValidationResult:
        """验证隔离区中的新字段"""
        conn = self.db.get_connection()

        # 获取隔离区中的样本
        rows = conn.execute(
            "SELECT sample_value FROM schema_quarantine "
            "WHERE table_name = ? AND field_name = ? AND status = 'pending'",
            (table, field_name)
        ).fetchall()

        if not rows:
            return ValidationResult(valid=False, reason="No samples found")

        # 验证规则
        samples = [json.loads(r["sample_value"]) for r in rows]

        # 1. 检查 null 率
        null_count = sum(1 for s in samples if s is None)
        null_rate = null_count / len(samples)
        if null_rate > 0.5:
            return ValidationResult(valid=False, reason=f"Null rate too high: {null_rate:.2%}")

        # 2. 检查数据类型一致性
        types = set(type(s).__name__ for s in samples if s is not None)
        if len(types) > 1:
            return ValidationResult(valid=False, reason=f"Inconsistent types: {types}")

        return ValidationResult(valid=True, samples=len(samples))

    def promote(self, table: str, field_name: str, field_type: str) -> PromotionResult:
        """将验证通过的字段晋升到主schema"""
        trace_id = str(uuid.uuid4())[:8]

        if not _IDENTIFIER_RE.match(table):
            return PromotionResult(success=False, error=QuantStarError(
                layer="data.evolution",
                severity=ErrorSeverity.WARNING,
                trace_id=trace_id,
                recoverable=False,
                suggestion=f"Invalid table name: {table!r}",
            ))
        if not _IDENTIFIER_RE.match(field_name):
            return PromotionResult(success=False, error=QuantStarError(
                layer="data.evolution",
                severity=ErrorSeverity.WARNING,
                trace_id=trace_id,
                recoverable=False,
                suggestion=f"Invalid field name: {field_name!r}",
            ))
        if field_type.upper() not in _VALID_FIELD_TYPES:
            return PromotionResult(success=False, error=QuantStarError(
                layer="data.evolution",
                severity=ErrorSeverity.WARNING,
                trace_id=trace_id,
                recoverable=False,
                suggestion=f"Invalid field type: {field_type!r}",
            ))

        conn = self.db.get_connection()
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {field_name} {field_type}")
            conn.execute(
                "UPDATE schema_quarantine SET status = 'promoted' "
                "WHERE table_name = ? AND field_name = ?",
                (table, field_name)
            )
            conn.commit()
            self._log_promotion(table, field_name, field_type)
            logger.info(f"Promoted field: {table}.{field_name}")
            return PromotionResult(success=True)
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to promote {table}.{field_name}: {e}")
            return PromotionResult(success=False, error=QuantStarError(
                layer="data.evolution",
                severity=ErrorSeverity.WARNING,
                trace_id=trace_id,
                recoverable=True,
                suggestion=str(e),
            ))

    def _get_table_columns(self, table: str) -> set[str]:
        """获取表的列名"""
        if not _IDENTIFIER_RE.match(table):
            return set()
        conn = self.db.get_connection()
        cursor = conn.execute(f"PRAGMA table_info({table})")
        return {row["name"] for row in cursor.fetchall()}

    def _infer_type(self, value: Any) -> str:
        """推断数据类型"""
        if value is None:
            return "TEXT"
        if isinstance(value, bool):
            return "INTEGER"
        if isinstance(value, int):
            return "INTEGER"
        if isinstance(value, float):
            return "REAL"
        return "TEXT"

    def _log_detection(self, table: str, field: str, field_type: str, source: str):
        """记录检测日志"""
        logger.info(f"Detected new field: {table}.{field} ({field_type}) from {source}")

    def _log_promotion(self, table: str, field: str, field_type: str):
        """记录晋升日志"""
        logger.info(f"Promoted field: {table}.{field} ({field_type})")

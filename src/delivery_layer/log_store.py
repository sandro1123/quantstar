"""F5.2c: delivery_log 推送日志持久化

表结构 (schema_version v3, 见 execution_layer/migrations.py 中央迁移清单):
    delivery_log(id, message_id, content_type, channel, status, error,
                 duration_ms, created_at)

DeliveryLogStore 只做写入 + 查询; 写入失败绝不阻塞投递本身。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from common.logging import get_logger
from delivery_layer.channels.base import DeliveryResult

if TYPE_CHECKING:
    from common.database import Database

logger = get_logger("delivery.log_store")

SECTION_TRACE_DDL = """
CREATE TABLE IF NOT EXISTS report_section_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL,
    section_index INTEGER NOT NULL,
    section_name TEXT NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    is_empty INTEGER NOT NULL DEFAULT 0,
    degraded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
)
"""

DELIVERY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS delivery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
)
"""


class DeliveryLogStore:
    def __init__(self, db: Database):
        self.db = db
        self.ensure_table()

    def ensure_table(self):
        """幂等建表 (新库自举; 旧库由 migrations v3 建, 二者 DDL 一致)"""
        conn = self.db.get_connection()
        conn.execute(DELIVERY_LOG_DDL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_log_created "
            "ON delivery_log (created_at)"
        )
        conn.execute(SECTION_TRACE_DDL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rst_ct_date "
            "ON report_section_trace (content_type, date)"
        )
        conn.commit()

    def record(self, result: DeliveryResult, content_type: str = ""):
        """落一条推送日志; 任何异常只告警不抛出"""
        try:
            conn = self.db.get_connection()
            conn.execute(
                "INSERT INTO delivery_log "
                "(message_id, content_type, channel, status, error, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (result.message_id, content_type, result.channel, result.status,
                 result.error_msg or "", result.duration_ms or 0),
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"delivery_log 写入失败 (不影响投递): {e}")

    # ── B席 ⑤: 晨报段留痕 (report_section_trace) ─────────────────────
    @staticmethod
    def _section_char_count(entry: dict) -> int:
        """段字符数 = title + subtitle + body 的字符数 (与渲染面同源)。"""
        return sum(len(str(entry.get(k) or "")) for k in ("title", "subtitle", "body"))

    def record_sections(self, message, date: str = "") -> None:
        """B席 ⑤: 落每段留痕 (段清单 + 字符数 + 空段/降级标记)。

        需求 (a) 段清单+字符数 (b) 零字符段显式标记 (c) 可查矩阵。
        纪律同 record: 任何异常只告警不抛出, 绝不阻塞投递。
        """
        try:
            # 判例#166 完整形态: 写侧必须和读侧镜像——读侧(_log_sections)读 raw_content,
            # 写侧这里也要先 raw_content, 否则 stash 形同虚设 (raw_content 不复用=修半不痛).
            content = getattr(message, "raw_content", None)
            if not isinstance(content, dict):
                content = getattr(message, "content", None)
            if not isinstance(content, dict):
                return
            sections = content.get("sections")
            if not isinstance(sections, list):
                return
            d = date or str(content.get("date") or "")
            ctype = getattr(message, "content_type", "") or ""
            mid = getattr(message, "message_id", "") or ""
            conn = self.db.get_connection()
            for idx, entry in enumerate(sections):
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or entry.get("title") or f"section_{idx}")
                cc = self._section_char_count(entry)
                conn.execute(
                    "INSERT INTO report_section_trace "
                    "(message_id, content_type, date, section_index, section_name, "
                    " char_count, is_empty, degraded) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (mid, ctype, d, idx, name, cc, 1 if cc == 0 else 0,
                     1 if entry.get("degraded") else 0),
                )
            conn.commit()
        except Exception as e:
            logger.warning(f"report_section_trace 写入失败 (不影响投递): {e}")

    def section_trace(self, date: str, content_type: str = "") -> list[dict]:
        """查某日段留痕 (按段序)。"""
        try:
            conn = self.db.get_connection()
            if content_type:
                rows = conn.execute(
                    "SELECT section_index, section_name, char_count, is_empty, degraded "
                    "FROM report_section_trace WHERE date = ? AND content_type = ? "
                    "ORDER BY section_index", (date, content_type)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT section_index, section_name, char_count, is_empty, degraded "
                    "FROM report_section_trace WHERE date = ? "
                    "ORDER BY section_index", (date,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def section_matrix(self, content_type: str = "morning_brief",
                       limit: int = 5) -> dict:
        """(c) 近 N 次逐段在位矩阵。

        返回 {runs: [日期...升序], sections: [段名...], matrix: {段名: [0/1...]}}。
        某段某次 char_count>0 → 1 (在位且有内容), 否则 0 (缺/空)。
        """
        try:
            conn = self.db.get_connection()
            runs = [r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM report_section_trace "
                "WHERE content_type = ? ORDER BY date DESC LIMIT ?",
                (content_type, limit)).fetchall()]
            runs = sorted(runs)
            if not runs:
                return {"runs": [], "sections": [], "matrix": {}}
            rows = conn.execute(
                "SELECT date, section_name, char_count FROM report_section_trace "
                "WHERE content_type = ? AND date >= ? ORDER BY date",
                (content_type, runs[0])).fetchall()
            names = sorted({r[1] for r in rows})
            matrix = {n: [0] * len(runs) for n in names}
            idx_of = {d: i for i, d in enumerate(runs)}
            for d, name, cc in rows:
                if (cc or 0) > 0:
                    matrix[name][idx_of[d]] = 1
            return {"runs": runs, "sections": names, "matrix": matrix}
        except Exception:
            return {"runs": [], "sections": [], "matrix": {}}

    def recent(self, limit: int = 20) -> list[dict]:
        """最近 N 条 (运维观测用)"""
        try:
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT message_id, content_type, channel, status, error, "
                "duration_ms, created_at FROM delivery_log "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

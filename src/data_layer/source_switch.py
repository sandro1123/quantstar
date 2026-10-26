"""data_layer/source_switch.py — DL-26 SourceSwitchRecord: 源切换留痕 (设计文档 §15/Q012)

每次管线降级 (主源失败 → 备源接管) 记录一条 SourceSwitchRecord,
使"主源失效"从静默变为可观测。配合 source_health 巡检 + AlertManager 告警,
构成完整的源健康闭环。

⚠️ 建表需走写库三步曲 → 报统筹 GO。本模块 record_source_switch() 不自动建表:
生产库无表时 INSERT 失败仅 warning (fail-safe, 不阻断取数); 测试自行建表。
DDL 见 _SCHEMA (供 GO 窗口执行)。

表结构:
  source_switch_records(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,          -- 切换发生时间 (北京时间)
    data_type TEXT NOT NULL,           -- 数据类型 (kline_bulk/limit_up/...)
    intended_source TEXT NOT NULL,     -- 首选源 (主源)
    actual_source TEXT NOT NULL,       -- 实际使用的源 (降级目标)
    reason TEXT NOT NULL DEFAULT '',   -- 切换原因 (异常信息/行数不足/超时)
    pipeline TEXT NOT NULL DEFAULT ''  -- 触发管线名
  )

使用:
  from data_layer.source_switch import record_source_switch, get_recent_switches
  record_source_switch(db, data_type="kline_bulk", intended="tushare",
                       actual="baostock", reason="墙钟超时 (120s)", pipeline="daily_klines")
"""

from __future__ import annotations

from common.logging import get_logger
from common.utils import bj_now

logger = get_logger("data.source_switch")

# DDL — 生产建表需 GO (写库三步曲), 不在本模块自动执行
_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_switch_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    data_type TEXT NOT NULL,
    intended_source TEXT NOT NULL,
    actual_source TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    pipeline TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ssr_created ON source_switch_records(created_at);
CREATE INDEX IF NOT EXISTS idx_ssr_dtype ON source_switch_records(data_type);
"""


def ensure_table(conn) -> None:
    """建表 (幂等) — 仅供测试/GO 窗口调用, 生产路径不自动建表。"""
    conn.executescript(_SCHEMA)


def record_source_switch(
    db,
    data_type: str,
    intended_source: str,
    actual_source: str,
    reason: str = "",
    pipeline: str = "",
) -> None:
    """记录一次源切换。失败不阻断管线 (fail-safe)。

    生产库无 source_switch_records 表时 (DDL 待 GO) 仅 warning, 不抛异常。
    """
    try:
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO source_switch_records "
            "(created_at, data_type, intended_source, actual_source, reason, pipeline) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bj_now(), data_type, intended_source, actual_source, reason, pipeline),
        )
        conn.commit()
        logger.info(
            f"SourceSwitch: {data_type} {intended_source}→{actual_source} ({reason[:80]})")
    except Exception as e:  # noqa: BLE001 — 记录失败不阻断取数 (含表不存在)
        logger.warning(f"record_source_switch failed (non-blocking): {e}")


def get_recent_switches(db, limit: int = 20) -> list[dict]:
    """查询最近的源切换记录 (运维报告/告警消费)。表不存在返回空列表。"""
    try:
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT * FROM source_switch_records ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

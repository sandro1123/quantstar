# -*- coding: utf-8 -*-
"""shortterm_recollect.py — 16:30 补采轮 (刀2, fix/anomaly-1630-recollect)

派单 sm-7c5089a73bd44eaf 第2件。病灶: 快照类端点 (anomaly-analysis-list
无日期参数最典型) 上游发布可能晚于 15:44 首轮窗 — 09-29 16:48 采得
200 行 vs 10-09 15:44 空; DL-07「不可回填, 错过当日永久失去」= 漏采
即数据永久缺口。10-19 anomaly 402 行坐实「当日更晚时刻数据在」,
补采轮把「在但漏」补回。

设计 (统筹核准两条):
1. 独立顶层小管道 (照 concept_plates_daily 先例): ops.yaml 16:30 排程,
   不进 DAILY_SUB_PIPELINES, GO-7 硬闸语义保持 (未注册=跳过+告警)。
   窗: DDP 实测 15:30→15:44 (~14min) 后 46min 隔离; 16:20/16:22
   DL-33 双门禁之后 (不扰门禁首轮读数)。
2. 幂等且不覆盖当日真值: INSERT OR REPLACE 语义下同日同键以补采轮为准
   (更晚快照), 但覆盖必须留痕 — 值级指纹 (非行数: 同键值变更行数不变,
   行数判据会漏, TDD test_overwrite_leaves_trace 实证), 指纹变 →
   metadata recollect_overwrote 显形, 不静默覆盖 (铁律#12)。
3. 只跑快照类六项 (ShortTermCollectPipeline._snapshot_collectors 单一
   事实源), 绝不碰 kline/valuation 主腿 — TDD 集合断言钉死。
"""
from __future__ import annotations

import hashlib

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.pipelines.shortterm_collect import ShortTermCollectPipeline, _SCHEMA

logger = get_logger("data.shortterm_recollect")

# 覆盖检测指纹口径: {表: (日期列, 内容表达式)} — 只依赖业务内容,
# 不依赖 created_at (UTC 时区且每次刷新, 拿它当指纹=每次都说「变了」)。
_FINGERPRINT_COLS = {
    "limit_up_ladder": ("snapshot_date",
                        "code||'|'||IFNULL(trade_date,'')||'|'||board_level"),
    "limit_break_pool": ("date", "code||'|'||IFNULL(name,'')"),
    "hot_stock_history": ("date",
                          "code||'|'||IFNULL(name,'')||'|'||IFNULL(rank,-1)"),
    "skyrocket_list": ("date",
                       "code||'|'||IFNULL(name,'')||'|'||IFNULL(rank,-1)"),
    "stock_anomaly_reasons": ("date", "code||'|'||IFNULL(tag_name,'')"),
    "sector_fund_flow": ("date", "name||'|'||IFNULL(sector_type,'')"),
}


def _table_fingerprint(conn, table: str, date_col: str, expr: str,
                       date: str) -> str | None:
    """当日既有行内容指纹 (md5[:12]); 无行 → None (纯补洞, 非覆盖)。"""
    try:
        rows = conn.execute(
            f"SELECT {expr} FROM {table} WHERE {date_col} = ? "
            f"ORDER BY {expr}", (date,)).fetchall()
        if not rows:
            return None
        blob = "\n".join(str(r[0]) for r in rows)
        return hashlib.md5(blob.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return None


class ShortTermRecollectPipeline(BasePipeline):
    """快照类六项 16:30 补采轮 — 复用 shortterm_collect 的采集器名单。"""

    name = "shortterm_recollect"
    requires = []
    produces = []  # 表清单与 shortterm_collect 相同 (COLLECT_TABLES), 不重复声明

    def __init__(self, db=None, source_registry=None):
        self.db = db
        self.source_registry = source_registry

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        conn = db.get_connection()
        conn.executescript(_SCHEMA)

        errors: list[str] = []
        total = 0
        collected: list[str] = []

        # 复用首轮同一份采集器 (单一事实源 — 只跑快照六项, 主腿不进)
        inner = ShortTermCollectPipeline(db=db, source_registry=self.source_registry)
        collectors = ShortTermCollectPipeline._snapshot_collectors()

        # 补采前指纹 (覆盖检测 — 值级, 治「同键值变更行数不变」漏报)
        pre_fp: dict[str, str | None] = {}
        for table, (col, expr) in _FINGERPRINT_COLS.items():
            pre_fp[table] = _table_fingerprint(conn, table, col, expr, input.date)

        for data_type, fn in collectors:
            try:
                n = fn(inner, conn, input.date)
                if n > 0:
                    total += n
                    collected.append(f"{data_type}:{n}")
                else:
                    errors.append(f"{data_type}: 补采无数据 (源仍不可用或非交易日)")
            except Exception as e:
                errors.append(f"{data_type}: 补采异常 {e}")
            conn.commit()

        # 覆盖留痕: 补采前有行 (指纹非 None) 且补采后指纹变 → 显形;
        # 补采前 0 行 (指纹 None) → 纯补洞不报; 指纹不变 → 同值不报
        # (防刷痕迹 — TDD test_same_value_no_false_overwrite_trace)。
        overwrote: list[str] = []
        for table, (col, expr) in _FINGERPRINT_COLS.items():
            post_fp = _table_fingerprint(conn, table, col, expr, input.date)
            if pre_fp[table] is not None and post_fp != pre_fp[table]:
                overwrote.append(
                    f"{table}: 指纹 {pre_fp[table]}→{post_fp} (补采轮为准)")

        status = "success" if not errors else ("partial" if total > 0 else "failed")
        logger.info(
            f"shortterm_recollect {input.date}: collected={collected} "
            f"overwrote={overwrote}")
        return PipelineResult(
            name=self.name, date=input.date, status=status,
            records_written=total, errors=errors,
            metadata={"collected": collected, "recollect_overwrote": overwrote},
        )

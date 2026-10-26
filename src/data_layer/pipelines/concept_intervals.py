"""概念/题材板块**区间存储**管道 — concept_membership + plate_daily_rate

背景 (统筹 sm-337ab3642f7f486f 批准, 用户拍板"待决策的123我都批"):
concept_daily 是快照表 — 每日全量展开"当日在板", 145k 行/日线性膨胀, 且读侧
get_concept_data 跨日 LIMIT 10 造成 concept_count 假活 (数的是跨日变化的名字,
不是当日真概念)。区间存储把成员关系压成 time_in/time_out 区间 (~145k 行总量,
不随天数膨胀), 板块涨幅按日一行 (~1018 行/日), 读侧改严格同日 PIT。

两张表 (DDL 单一事实源在本模块, scripts/create_concept_interval_tables.py 引用):

- concept_membership(code, concept_name, plate_code, plate_type, source,
  time_in, time_out): PK (code, plate_code, time_in) — 出板事件表现为旧行
  time_out 由 NULL 变有值, INSERT OR REPLACE 幂等覆盖;
- plate_daily_rate(plate_code, plate_type, date, rate, source): PK (plate_code, date)。

统筹五约束:
① plate_type 三态留痕 (source 列 15/17 分开打标, 适配器侧);
② change_pct/rate 拿不到写 NULL, 禁止 0.0 (F-45 教训);
③ stock_info 宇宙过滤 + 落日志 (341 事件探针);
④ 空板块跳过 + 落日志 (适配器侧计数);
⑤ **建表 DDL 所有列 DEFAULT 一律 NULL** — 不写任何 DEFAULT 子句
   (add_adjust_type_column.py DEFAULT 'qfq' 教训: 宁可 NULL 不可给看起来合理的假值)。

索引 idx_mem_code(code, time_in): 实测读侧 238× 加速 (45.29s → 0.19s)。

🔴 GO-7 硬闸: 同 concept_plates — 只注册进 PipelineRegistry, **不接进
orchestrator sub_pipelines** (写锁血栓排期未落地)。生产建表/写入/回补
(D-104 幸存者偏差待决) 均需另行 GO; 本管道代码+测试只在 worktree。
"""
import time

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.concept_intervals")

# ---------------------------------------------------------------------------
# DDL 单一事实源 — 约束⑤: 不写任何 DEFAULT 子句 (SQLite 缺省即 NULL)。
# NOT NULL 只加在 PK 列上 (主键约束, 不是"看起来合理的默认值")。
# ---------------------------------------------------------------------------
CONCEPT_MEMBERSHIP_DDL = """
CREATE TABLE IF NOT EXISTS concept_membership (
    code TEXT NOT NULL,
    concept_name TEXT NOT NULL,
    plate_code TEXT NOT NULL,
    plate_type INTEGER,
    source TEXT,
    time_in TEXT NOT NULL,
    time_out TEXT,
    PRIMARY KEY (code, plate_code, time_in)
)
"""

PLATE_DAILY_RATE_DDL = """
CREATE TABLE IF NOT EXISTS plate_daily_rate (
    plate_code TEXT NOT NULL,
    plate_type INTEGER,
    date TEXT NOT NULL,
    rate REAL,
    source TEXT,
    PRIMARY KEY (plate_code, date)
)
"""

# 读侧主查询路径: 按 (code, time_in) 前缀命中, 实测 238× 加速。
CONCEPT_INTERVAL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_mem_code ON concept_membership(code, time_in)",
)


class ConceptIntervalsPipeline(BasePipeline):
    name = "concept_intervals"
    requires: list[str] = []
    produces = ["concept_membership", "plate_daily_rate"]

    def __init__(self, db=None, source_registry=None):
        self.db = db
        self.source_registry = source_registry

    # ------------------------------------------------------------------
    # 取数: SourceRegistry 优先, 兜底直连 zzshare 适配器 (同 concept_plates)
    # ------------------------------------------------------------------

    def _fetch_intervals(self, params: dict) -> tuple[dict | None, str]:
        if self.source_registry:
            for source in self.source_registry.get_sources_for("concept_intervals"):
                try:
                    data = source.get_data("concept_intervals", params)
                    if data:
                        logger.info(f"concept_intervals from {source.name}")
                        return data, source.name
                except Exception as e:
                    logger.debug(f"Source {source.name} failed for concept_intervals: {e}")
                    continue
        from data_layer.sources.zzshare_source import ZZShareSourceAdapter
        adapter = ZZShareSourceAdapter()
        try:
            return adapter.get_data("concept_intervals", params), adapter.name
        except Exception as e:
            logger.warning(f"zzshare concept_intervals failed: {e}")
            return None, "zzshare"

    @staticmethod
    def _norm_code(code) -> str:
        s = str(code or "").strip()
        return s.zfill(6) if s.isdigit() else s

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        conn = db.get_connection()
        errors: list[str] = []
        warnings: list[str] = []

        # 幂等 DDL (dry_run 基类透传 CREATE; 约束⑤ 无 DEFAULT 子句)
        conn.execute(CONCEPT_MEMBERSHIP_DDL)
        conn.execute(PLATE_DAILY_RATE_DDL)
        for idx_ddl in CONCEPT_INTERVAL_INDEXES:
            conn.execute(idx_ddl)

        backfill = bool(input.config.get("backfill", False))
        sleep_s = float(input.config.get("sleep_s", 0.05))

        # 1. 拉取 (慢步骤 ~2.5min/日, 在写事务之外 — 不持写锁)
        t_fetch = time.time()
        data, source_name = self._fetch_intervals({
            "date": input.date, "backfill": backfill, "sleep_s": sleep_s,
        })
        fetch_duration_s = time.time() - t_fetch
        memberships = (data or {}).get("memberships") or []
        rates = (data or {}).get("rates") or []
        stats = (data or {}).get("stats") or {}
        if not memberships:
            errors.append(
                f"concept_intervals 拉取 0 条成员区间 (source={source_name}, "
                f"stats={stats}) — fail loud, 不静默写空 (移交A/DL-26 教训)")
            return PipelineResult(
                name=self.name, date=input.date, status="failed",
                errors=errors, duration_s=fetch_duration_s,
                metadata={"fetch_duration_s": round(fetch_duration_s, 2),
                          "source": source_name})

        # 2. stock_info 宇宙过滤 (约束③: 341 事件探针 — 宇宙事故时宁可不写)
        try:
            rows = conn.execute("SELECT code FROM stock_info").fetchall()
        except Exception as e:
            errors.append(f"stock_info 读取失败: {e}")
            rows = []
        universe = {self._norm_code(r[0]) for r in rows if r[0]}
        if not universe:
            errors.append(
                "stock_info 宇宙为空 — 拒绝写库 (341 事件教训: 宇宙事故时"
                "宁可不写也不写错)")
            return PipelineResult(
                name=self.name, date=input.date, status="failed",
                records_read=len(memberships), errors=errors,
                metadata={"fetch_duration_s": round(fetch_duration_s, 2),
                          "universe_size": 0, "source": source_name})

        kept: list[tuple] = []
        filtered_out = 0
        for m in memberships:
            code = self._norm_code(m.get("code"))
            if code not in universe:
                filtered_out += 1
                continue
            kept.append((
                code,
                m.get("concept_name") or "",
                m.get("plate_code") or "",
                m.get("plate_type"),
                m.get("source"),
                m.get("time_in"),
                m.get("time_out"),  # None → NULL (仍在板)
            ))
        if filtered_out:
            warnings.append(
                f"stock_info 宇宙过滤掉 {filtered_out} 条成员区间 "
                f"(宇宙 {len(universe)} 股)")
            logger.warning(
                f"concept_intervals {input.date}: filtered_out={filtered_out}, "
                f"universe={len(universe)}")
        if not kept:
            errors.append("过滤后 0 条可写成员区间 — 拒绝写库")
            return PipelineResult(
                name=self.name, date=input.date, status="failed",
                records_read=len(memberships), errors=errors, warnings=warnings,
                metadata={"fetch_duration_s": round(fetch_duration_s, 2),
                          "universe_size": len(universe),
                          "filtered_out": filtered_out,
                          "source": source_name})

        rate_rows = [
            (r.get("plate_code"), r.get("plate_type"), r.get("date"),
             r.get("rate"),  # None → NULL, 禁止 0.0 (约束②)
             r.get("source"))
            for r in rates
        ]
        if not rate_rows:
            warnings.append(
                "plate_daily_rate 当日 0 行 — plates_rank 可能整体失败 "
                f"(stats.errors={stats.get('errors', [])[:3]})")

        # 3. 写库 — 单次事务, 写锁持有时间计时 (GO-7 三数之一)
        t_write = time.time()
        try:
            # 幂等覆盖: 出板事件 = 旧行 time_out 由 NULL 变有值
            conn.executemany(
                "INSERT OR REPLACE INTO concept_membership "
                "(code, concept_name, plate_code, plate_type, source, time_in, time_out) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                kept)
            conn.executemany(
                "INSERT OR REPLACE INTO plate_daily_rate "
                "(plate_code, plate_type, date, rate, source) VALUES (?, ?, ?, ?, ?)",
                rate_rows)
            conn.commit()
        except Exception as e:
            conn.rollback()
            errors.append(f"写库失败: {e}")
            return PipelineResult(
                name=self.name, date=input.date, status="failed",
                records_read=len(memberships), errors=errors, warnings=warnings,
                metadata={"fetch_duration_s": round(fetch_duration_s, 2)})
        write_lock_hold_s = time.time() - t_write

        if stats.get("errors"):
            warnings.append(f"源侧错误 {len(stats['errors'])} 条: {stats['errors'][:3]}")
        status = "success" if not errors else "partial"
        logger.info(
            f"concept_intervals {input.date}: memberships={len(kept)}, "
            f"rates={len(rate_rows)}, filtered_out={filtered_out}, "
            f"fetch={fetch_duration_s:.1f}s, write_lock={write_lock_hold_s:.2f}s, "
            f"backfill={backfill}")
        return PipelineResult(
            name=self.name, date=input.date, status=status,
            records_read=len(memberships), records_written=len(kept) + len(rate_rows),
            errors=errors, warnings=warnings,
            metadata={
                "source": source_name,
                "backfill": backfill,
                "universe_size": len(universe),
                "filtered_out": filtered_out,
                "memberships_written": len(kept),
                "rates_written": len(rate_rows),
                "rank_missing": stats.get("rank_missing", 0),
                "time_in_missing": stats.get("time_in_missing", 0),
                "fetch_duration_s": round(fetch_duration_s, 2),
                "write_lock_hold_s": round(write_lock_hold_s, 3),
                "plates_total": stats.get("plates_total", 0),
                "plates_ok": stats.get("plates_ok", 0),
                "plates_empty": stats.get("plates_empty", 0),
            })

"""概念/题材板块管道 (DL-49) — zzshare plates 正向映射, 不依赖涨停

背景 (DL-49 调查结论): concept_daily 的"真概念"字段契约耦合在 zzshare
review_uplimit_reason 的板块嵌套响应形状上, 换源 fuyao 后静默归零, 行业兜底
冒充概念 (F-45: concept_count 恒 0.1 常量)。本管道用 plates_list +
plates_stocks + plates_rank 正向映射恢复真概念:

- 覆盖全市场 (实测 7389 股, 80.2% 一股多板块), 不再依赖涨停的结构性覆盖缺口;
- plate_type 15(概念)/17(题材) 分开采并留痕 source 列 (前置②);
- change_pct 来自 plates_rank.rate, 映射没有的写 NULL 不写 0.0 (前置③, F-45 教训);
- 时点在板判定 (time_in<=date, time_out 空或 >date), 历史可回溯 (实测至 2020-01);
- 回补打标: config["backfill"]=True → source 加 _backfill 后缀 (统筹裁定:
  plates_list 只给当前板块目录, 回补段有幸存者偏差, IC 分段/权重只用无偏段);
- stock_info 宇宙过滤 + 计数 (统筹附加②: stock_info 事故的探针, 341 事件教训);
- 真概念写入当日先清掉该日的 industry_fallback 行 (行业不再冒充概念);
- 写库集中在单次事务, 写锁持有时间落 metadata (GO-7 排期要的三数之一)。

🔴 GO-7 硬闸: 本管道只注册进 PipelineRegistry, **不接进 orchestrator 的
sub_pipelines** (写锁血栓: 日管道持写锁 22-40min 撞崩 factor_update)。
接入日管道等 GO-7 排期落地。测试钉住此约束。
"""
import time

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.concept_plates")


class ConceptPlatesPipeline(BasePipeline):
    name = "concept_plates"
    requires: list[str] = []
    produces = ["concept_daily"]

    def __init__(self, db=None, source_registry=None):
        self.db = db
        self.source_registry = source_registry

    # ------------------------------------------------------------------
    # 取数: SourceRegistry 优先, 兜底直连 zzshare 适配器
    # ------------------------------------------------------------------

    def _fetch_plates(self, params: dict) -> tuple[dict | None, str]:
        if self.source_registry:
            for source in self.source_registry.get_sources_for("concept_plates"):
                try:
                    data = source.get_data("concept_plates", params)
                    if data:
                        logger.info(f"concept_plates from {source.name}")
                        return data, source.name
                except Exception as e:
                    logger.debug(f"Source {source.name} failed for concept_plates: {e}")
                    continue
        from data_layer.sources.zzshare_source import ZZShareSourceAdapter
        adapter = ZZShareSourceAdapter()
        try:
            return adapter.get_data("concept_plates", params), adapter.name
        except Exception as e:
            logger.warning(f"zzshare concept_plates failed: {e}")
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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_daily (
                code TEXT, date TEXT, concept_name TEXT, change_pct REAL,
                source TEXT DEFAULT '',
                PRIMARY KEY (code, date, concept_name)
            )
        """)

        backfill = bool(input.config.get("backfill", False))
        sleep_s = float(input.config.get("sleep_s", 0.05))

        # 1. 拉取 (慢步骤, ~2.5min/日, 在写事务之外 — 不持写锁)
        t_fetch = time.time()
        data, source_name = self._fetch_plates({
            "date": input.date, "backfill": backfill, "sleep_s": sleep_s,
        })
        fetch_duration_s = time.time() - t_fetch
        if not data or not data.get("records"):
            stats = (data or {}).get("stats", {})
            errors.append(
                f"concept_plates 拉取 0 行 (source={source_name}, "
                f"stats={stats}) — fail loud, 不静默写空 (移交A/DL-26 教训)")
            return PipelineResult(
                name=self.name, date=input.date, status="failed",
                errors=errors, duration_s=fetch_duration_s,
                metadata={"fetch_duration_s": round(fetch_duration_s, 2),
                          "source": source_name})
        records = data["records"]
        stats = data.get("stats", {})

        # 2. stock_info 宇宙过滤 (统筹附加②: 341 事件探针 — stock_info 被抹空时
        #    这里会过滤掉全部记录, 宇宙行数落 metadata 供告警)
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
                records_read=len(records), errors=errors,
                metadata={"fetch_duration_s": round(fetch_duration_s, 2),
                          "universe_size": 0, "source": source_name})

        kept: list[tuple] = []
        filtered_out = 0
        for rec in records:
            code = self._norm_code(rec.get("code"))
            if code not in universe:
                filtered_out += 1
                continue
            kept.append((
                code, input.date, rec.get("concept_name", ""),
                rec.get("change_pct"),  # None → NULL, 禁止 0.0 (前置③)
                rec.get("source", ""),
            ))
        if filtered_out:
            warnings.append(
                f"stock_info 宇宙过滤掉 {filtered_out} 条记录 "
                f"(宇宙 {len(universe)} 股)")
            logger.warning(
                f"concept_plates {input.date}: filtered_out={filtered_out}, "
                f"universe={len(universe)}")
        if not kept:
            errors.append("过滤后 0 条可写记录 — 拒绝写库")
            return PipelineResult(
                name=self.name, date=input.date, status="failed",
                records_read=len(records), errors=errors, warnings=warnings,
                metadata={"fetch_duration_s": round(fetch_duration_s, 2),
                          "universe_size": len(universe),
                          "filtered_out": filtered_out,
                          "source": source_name})

        # 3. 写库 — 单次事务, 写锁持有时间计时 (GO-7 三数之一)
        t_write = time.time()
        try:
            # 真概念到场 → 该日行业兜底行退场 (行业不再冒充概念)
            deleted = conn.execute(
                "DELETE FROM concept_daily WHERE date = ? AND source = 'industry_fallback'",
                (input.date,)).rowcount
            conn.executemany(
                "INSERT OR REPLACE INTO concept_daily "
                "(code, date, concept_name, change_pct, source) VALUES (?, ?, ?, ?, ?)",
                kept)
            conn.commit()
        except Exception as e:
            conn.rollback()
            errors.append(f"写库失败: {e}")
            return PipelineResult(
                name=self.name, date=input.date, status="failed",
                records_read=len(records), errors=errors, warnings=warnings,
                metadata={"fetch_duration_s": round(fetch_duration_s, 2)})
        write_lock_hold_s = time.time() - t_write

        if stats.get("errors"):
            warnings.append(f"源侧错误 {len(stats['errors'])} 条: {stats['errors'][:3]}")
        status = "success" if not errors else "partial"
        logger.info(
            f"concept_plates {input.date}: written={len(kept)}, "
            f"deleted_fallback={deleted}, filtered_out={filtered_out}, "
            f"fetch={fetch_duration_s:.1f}s, write_lock={write_lock_hold_s:.2f}s, "
            f"backfill={backfill}")
        return PipelineResult(
            name=self.name, date=input.date, status=status,
            records_read=len(records), records_written=len(kept),
            errors=errors, warnings=warnings,
            metadata={
                "source": source_name,
                "backfill": backfill,
                "universe_size": len(universe),
                "filtered_out": filtered_out,
                "deleted_fallback_rows": deleted,
                "fetch_duration_s": round(fetch_duration_s, 2),
                "write_lock_hold_s": round(write_lock_hold_s, 3),
                "plates_total": stats.get("plates_total", 0),
                "plates_ok": stats.get("plates_ok", 0),
                "plates_empty": stats.get("plates_empty", 0),
                "rank_missing": stats.get("rank_missing", 0),
            })

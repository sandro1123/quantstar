"""分析师预测数据管道 — 从 SourceRegistry 获取研报预测（多源 fallback + 全市场覆盖）"""
from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.analyst_forecast")


class AnalystForecastPipeline(BasePipeline):
    name = "analyst_forecasts"
    requires = []
    produces = ["analyst_forecasts"]

    def __init__(self, db=None, source_registry=None, max_codes: int = 0):
        self.db = db
        self.source_registry = source_registry
        self.max_codes = max_codes

    def _fetch_for_code(self, code: str):
        if self.source_registry:
            for source in self.source_registry.get_sources_for("analyst_forecast"):
                try:
                    rec = source.get_data("analyst_forecast", {"code": code})
                    if rec:
                        return rec
                except Exception as e:
                    logger.debug(f"Source {source.name} failed for analyst_forecast {code}: {e}")
                    continue
        else:
            from data_layer.sources.akshare_source import AkShareSourceAdapter
            try:
                return AkShareSourceAdapter().get_data("analyst_forecast", {"code": code})
            except Exception:
                return None
        return None

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        conn = db.get_connection()
        errors = []
        total = 0

        conn.execute("""
            CREATE TABLE IF NOT EXISTS analyst_forecasts (
                code TEXT, date TEXT, consensus_score REAL DEFAULT 0,
                target_price REAL DEFAULT 0, rating TEXT DEFAULT '',
                PRIMARY KEY (code, date)
            )
        """)

        codes = [r[0] for r in conn.execute("SELECT code FROM stock_info WHERE is_active = 1 AND code NOT LIKE '4%' AND code NOT LIKE '8%'").fetchall()]
        if not codes:
            codes = ["000001", "000002", "000005", "600519"]

        # 全市场覆盖（移除历史 [:200] 截断）；max_codes>0 时作为保护上限
        if self.max_codes > 0:
            codes = codes[: self.max_codes]

        for code in codes:
            try:
                rec = self._fetch_for_code(code)
                if not rec:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO analyst_forecasts (code, date, consensus_score, target_price, rating) VALUES (?, ?, ?, ?, ?)",
                    (code, input.date, rec.get("consensus_score", 0.0), rec.get("target_price", 0.0), rec.get("rating", "")),
                )
                total += 1
            except Exception:
                continue

        coverage = total / len(codes) if codes else 0.0
        logger.info(f"analyst_forecasts: {total}/{len(codes)} codes covered ({coverage:.1%})")
        if total == 0:
            errors.append(f"analyst: 所有源均无数据 (尝试 {len(codes)} 只)")
        conn.commit()
        status = "success" if not errors else "partial"
        return PipelineResult(name=self.name, date=input.date, status=status,
                              records_written=total, errors=errors,
                              metadata={"coverage": round(coverage, 4), "attempted": len(codes)})

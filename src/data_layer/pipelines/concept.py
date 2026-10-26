"""概念板块数据管道 — 从 SourceRegistry 获取板块/概念成分股信息（多源 fallback）"""
from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.concept")


class ConceptPipeline(BasePipeline):
    name = "concept_daily"
    requires: list[str] = []
    produces = ["concept_daily"]

    def __init__(self, db=None, source_registry=None):
        self.db = db
        self.source_registry = source_registry

    def _fetch(self, data_type: str, params: dict):
        if not self.source_registry:
            from data_layer.sources.zzshare_source import ZZShareSourceAdapter
            try:
                return ZZShareSourceAdapter().get_data(data_type, params)
            except Exception as e:
                logger.debug(f"zzshare {data_type} failed: {e}")
                return None
        for source in self.source_registry.get_sources_for(data_type):
            try:
                data = source.get_data(data_type, params)
                if data:
                    logger.info(f"concept {data_type} from {source.name}")
                    return data
            except Exception as e:
                logger.debug(f"Source {source.name} failed for {data_type}: {e}")
                continue
        return None

    @staticmethod
    def _ensure_source_column(conn):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(concept_daily)").fetchall()]
        if "source" not in cols:
            try:
                conn.execute("ALTER TABLE concept_daily ADD COLUMN source TEXT DEFAULT ''")
                conn.commit()
            except Exception:
                pass

    def _insert(self, conn, code: str, date: str, name: str, fallback: bool) -> int:
        conn.execute(
            "INSERT OR IGNORE INTO concept_daily (code, date, concept_name, change_pct, source) "
            "VALUES (?, ?, ?, 0.0, ?)",
            (code, date, name, "industry_fallback" if fallback else "concept"),
        )
        return 1

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        conn = db.get_connection()
        errors = []
        total = 0

        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_daily (
                code TEXT, date TEXT, concept_name TEXT, change_pct REAL,
                source TEXT DEFAULT '',
                PRIMARY KEY (code, date, concept_name)
            )
        """)
        self._ensure_source_column(conn)

        # 1. 从涨停原因获取板块-股票映射（真实概念）
        try:
            limit_data = self._fetch("limit_up", {"date": input.date})
            if limit_data:
                for plate in limit_data:
                    name = plate.get("plate_name", "")
                    for stock in plate.get("stocks", []):
                        code = stock.get("stock_code", "")
                        if code and name:
                            total += self._insert(conn, code, input.date, name, fallback=False)
        except Exception as e:
            errors.append(f"涨停来源: {e}")

        # 2. 从 stock_basic 补充行业信息（行业冒充概念 → 标记 fallback）
        try:
            stocks = self._fetch("stock_basic", {})
            if stocks:
                for s in stocks:
                    code = s.get("symbol", s.get("code", ""))
                    industry = s.get("industry", "") or ""
                    if industry and code:
                        total += self._insert(conn, code, input.date, industry, fallback=True)
        except Exception as e:
            errors.append(f"行业来源: {e}")

        # 3. 从 stock_info 补充行业数据（同上标记 fallback）
        try:
            rows = conn.execute("SELECT code, industry FROM stock_info WHERE industry IS NOT NULL AND industry != ''").fetchall()
            for r in rows:
                total += self._insert(conn, r[0], input.date, r[1], fallback=True)
        except Exception as e:
            errors.append(f"stock_info: {e}")

        conn.commit()
        status = "success" if not errors else "partial"
        return PipelineResult(name=self.name, date=input.date, status=status, records_written=total, errors=errors)

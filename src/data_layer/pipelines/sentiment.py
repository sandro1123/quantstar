"""市场情绪数据管道 — 从 SourceRegistry 获取涨跌分布/情绪指标（多源 fallback）"""
from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.sentiment")


class SentimentPipeline(BasePipeline):
    name = "market_sentiment"
    requires = []
    produces = ["market_sentiment"]

    def __init__(self, db=None, source_registry=None):
        self.db = db
        self.source_registry = source_registry

    def _fetch(self, date: str):
        if not self.source_registry:
            from data_layer.sources.zzshare_source import ZZShareSourceAdapter
            try:
                return ZZShareSourceAdapter().get_data("sentiment", {"date": date}), "zzshare"
            except Exception as e:
                logger.debug(f"zzshare sentiment failed: {e}")
                return None, ""
        for source in self.source_registry.get_sources_for("sentiment"):
            try:
                data = source.get_data("sentiment", {"date": date})
                if data:
                    logger.info(f"sentiment from {source.name}")
                    return data, source.name
            except Exception as e:
                logger.debug(f"Source {source.name} failed for sentiment: {e}")
                continue
        return None, ""

    def _compute_local_fallback(self, db, date: str) -> dict | None:
        """DL-14: 从本地可靠表计算情绪兜底 (源失败时)。

        涨跌家数取 daily_klines.change_pct 符号; 涨跌停数取
        stock_limit_up_down.limit_type (limit_up/limit_down)。
        返回与 zzshare 同构的 {"info": {...}} 供 run() 统一解析。
        无任何本地数据 (非交易日/表缺失) 返回 None。
        """
        try:
            conn = db.get_connection()
            up = conn.execute(
                "SELECT COUNT(*) FROM daily_klines WHERE date = ? AND change_pct > 0",
                (date,)).fetchone()[0]
            down = conn.execute(
                "SELECT COUNT(*) FROM daily_klines WHERE date = ? AND change_pct < 0",
                (date,)).fetchone()[0]
            try:
                zt = conn.execute(
                    "SELECT COUNT(*) FROM stock_limit_up_down "
                    "WHERE date = ? AND limit_type = 'limit_up'", (date,)).fetchone()[0]
                dt = conn.execute(
                    "SELECT COUNT(*) FROM stock_limit_up_down "
                    "WHERE date = ? AND limit_type = 'limit_down'", (date,)).fetchone()[0]
            except Exception:
                zt = dt = 0
            if up == 0 and down == 0 and zt == 0 and dt == 0:
                return None  # 非交易日/无数据, 不产出空行
            return {"info": {"sign": "0", "ZT": zt, "DT": dt,
                             "SJZT": up, "SJDT": down}}
        except Exception as e:
            logger.debug(f"sentiment local fallback failed: {e}")
            return None

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        from data_layer._models import SentimentData
        conn = db.get_connection()
        errors = []
        total = 0

        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_sentiment (
                date TEXT PRIMARY KEY,
                market_temp REAL DEFAULT 0,
                limit_up_count INTEGER DEFAULT 0,
                limit_down_count INTEGER DEFAULT 0,
                up_count INTEGER DEFAULT 0,
                down_count INTEGER DEFAULT 0,
                zt_count INTEGER DEFAULT 0,
                dt_count INTEGER DEFAULT 0
            )
        """)

        data, _src = self._fetch(input.date)
        if not data:
            # DL-14: 源全部失败 (zzshare 不可靠) → 本地表计算兜底, 保证连续产出
            data = self._compute_local_fallback(db, input.date)
            if data:
                _src = "local_fallback"
        if data and isinstance(data, dict):
            info = data.get("info", {})
            sign_str = info.get("sign", "0")
            try:
                market_temp = float(sign_str)
            except ValueError:
                market_temp = 0.0
            sentiment = SentimentData(
                date=input.date,
                market_temp=market_temp,
                limit_up_count=int(info.get("ZT", 0) or 0),
                limit_down_count=int(info.get("DT", 0) or 0),
                up_count=int(info.get("SJZT", 0) or 0),
                down_count=int(info.get("SJDT", 0) or 0),
                zt_count=int(info.get("ZT", 0) or 0),
                dt_count=int(info.get("DT", 0) or 0),
            )
            conn.execute(
                "INSERT OR REPLACE INTO market_sentiment VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sentiment.date, sentiment.market_temp, sentiment.limit_up_count,
                 sentiment.limit_down_count, sentiment.up_count, sentiment.down_count,
                 sentiment.zt_count, sentiment.dt_count),
            )
            total = 1
        else:
            errors.append("情绪: 所有源均无数据")

        conn.commit()
        status = "success" if not errors else "partial"
        return PipelineResult(name=self.name, date=input.date, status=status, records_written=total, errors=errors)

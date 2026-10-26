"""AdataSentimentPipeline — 人气榜+热门概念冷采集 (卡#20 第一段, 夜间窗纪律)。

两表 (b 案单腿口径):
  popularity_daily (date, code, rank, his_rank, source) — 人气榜 100, 价格列
    缺省诚实化 (push2 域被墙死, 统筹 sm-3f5181e5931d4ae8 裁 b 案)
  hot_concept_daily (date, rank, concept_code, concept_name, change_pct,
    hot_value, hot_tag, source) — 同花顺热门概念 20
喂 ST-REGIME 情绪温度计 (shortterm_data 同族原料)。
源不可用 → 诚实降级 (degraded=True, 零行, 不造假)。
"""
from common.logging import get_logger
from common.utils import bj_date

logger = get_logger("data.pipelines.adata_sentiment")

POPULARITY_DDL = """
CREATE TABLE IF NOT EXISTS popularity_daily (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    rank INTEGER,
    his_rank REAL,
    source TEXT DEFAULT 'adata',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (date, code)
)
"""

HOT_CONCEPT_DDL = """
CREATE TABLE IF NOT EXISTS hot_concept_daily (
    date TEXT NOT NULL,
    rank INTEGER,
    concept_code TEXT,
    concept_name TEXT,
    change_pct REAL,
    hot_value REAL,
    hot_tag TEXT,
    source TEXT DEFAULT 'adata',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (date, concept_code)
)
"""


class AdataSentimentPipeline:
    """adata 情绪族冷采集 — 人气榜 + 热门概念 (夜间窗拉取纪律)。"""

    def __init__(self, db=None, source_registry=None):
        self.db = db
        self.source_registry = source_registry
        self.name = "adata_sentiment"

    def run(self, params: dict) -> dict:
        date = params.get("date") or bj_date()
        result = {"date": date, "popularity_rows": 0, "concept_hot_rows": 0,
                  "degraded": False}

        source = (self.source_registry.get_best_source("popularity")
                  if self.source_registry else None)
        if source is None:
            logger.warning("adata_sentiment: 无可用 popularity 源, 诚实降级零行")
            result["degraded"] = True
            return result

        conn = self.db.get_connection() if self.db else None
        if conn is None:
            result["degraded"] = True
            return result

        conn.execute(POPULARITY_DDL)
        conn.execute(HOT_CONCEPT_DDL)

        # 人气榜 (b 案单腿)
        pop = source.get_data("popularity", {"limit": 100}) or []
        for r in pop:
            conn.execute(
                "INSERT OR REPLACE INTO popularity_daily "
                "(date, code, rank, his_rank, source) VALUES (?, ?, ?, ?, ?)",
                (date, r.get("code"), r.get("rank"), r.get("his_rank"),
                 r.get("source", "adata")))
        result["popularity_rows"] = len(pop)

        # 热门概念
        hot = source.get_data("concept_hot", {}) or []
        for r in hot:
            conn.execute(
                "INSERT OR REPLACE INTO hot_concept_daily "
                "(date, rank, concept_code, concept_name, change_pct, hot_value, "
                "hot_tag, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (date, r.get("rank"), r.get("concept_code"),
                 r.get("concept_name"), r.get("change_pct"), r.get("hot_value"),
                 r.get("hot_tag", ""), r.get("source", "adata")))
        result["concept_hot_rows"] = len(hot)

        if self.db:
            try:
                conn.commit()
            except Exception:
                pass
        logger.info(
            f"adata_sentiment {date}: popularity {len(pop)} 行 + "
            f"concept_hot {len(hot)} 行"
            + (" (degraded)" if result["degraded"] else ""))
        return result

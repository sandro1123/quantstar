"""pipelines/auction_collect.py — DL-07 集合竞价快照冷采集

竞价数据是盘中快照型, 错过即永久失去 (用户预决策 #7)。
挂 auction_parse (9:25) 执行: 先拉短线风向标竞价基准 (全市场风向标股),
再对这些标的拉竞价快照落库。冷采集 + 最低治理, 不建因子不进选股链路。

端点 (DL-00 实测, 正确路径在 /api/a-share/auction/ 下):
- auction/short-term-benchmark  竞价风向标 → auction_benchmark
- auction/snapshot              竞价快照   → auction_snapshot (按 thscodes 批量)
"""

from __future__ import annotations

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.auction_collect")

AUCTION_TABLES = ["auction_benchmark", "auction_snapshot"]

# AUD-002 警示: 下面 DDL 的时间列默认是 **UTC** (CURRENT_TIMESTAMP /
# datetime('now')), 与全库 BJT 主流 (datetime('now','+8 hours')) 不同。
# 加日期过滤、时间窗查询或渲染给人看之前 **必须先转换**, 否则差 8 小时。
# 禁止只把这里的默认值改成 BJT —— 那会让新行 BJT、存量行 UTC 落进同一列,
# 从「一致的 UTC(安全)」退化成「同列混写(静默出错)」; 要改须连存量一起平移。
# 定谳与三路方案见 docs/audit/OPUS5_DEEP_AUDIT.md 的 AUD-002 段。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS auction_benchmark (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    auction_pct REAL,
    tags TEXT,
    source TEXT DEFAULT 'fuyao',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS auction_snapshot (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    auction_price REAL,
    auction_pct REAL,
    auction_volume REAL,
    auction_amount REAL,
    auction_unmatched REAL,
    auction_turnover_pct REAL,
    source TEXT DEFAULT 'fuyao',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, code)
);
"""


def _code_from_thscode(thscode: str) -> str:
    return thscode.split(".")[0] if thscode else ""


def _safe_float(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


class AuctionCollectPipeline(BasePipeline):
    """集合竞价冷采集 — auction_parse (9:25) 执行。"""

    name = "auction_collect"
    requires = []
    produces = AUCTION_TABLES

    def __init__(self, db=None, source_registry=None):
        self.db = db
        self.source_registry = source_registry

    def _fetch(self, data_type: str, params: dict) -> tuple[object, str]:
        if not self.source_registry:
            return None, ""
        for source in self.source_registry.get_sources_for(data_type):
            try:
                data = source.get_data(data_type, params)
                if data:
                    return data, source.name
            except Exception as e:
                logger.debug(f"source {source.name} failed for {data_type}: {e}")
                continue
        return None, ""

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        conn = db.get_connection()
        conn.executescript(_SCHEMA)

        errors: list[str] = []
        total = 0
        thscodes: list[str] = []

        # 1. 竞价风向标 (全市场, 无需参数)
        bench, _src = self._fetch("auction_benchmark", {})
        if bench:
            for r in bench:
                code = r.get("ticker") or _code_from_thscode(r.get("thscode", ""))
                if not code:
                    continue
                if r.get("thscode"):
                    thscodes.append(r["thscode"])
                tags = r.get("tags") or []
                conn.execute(
                    "INSERT OR REPLACE INTO auction_benchmark "
                    "(date, code, name, auction_pct, tags) VALUES (?, ?, ?, ?, ?)",
                    (input.date, code, r.get("name", ""),
                     _safe_float(r.get("auction_pct")),
                     ",".join(tags) if isinstance(tags, list) else str(tags)),
                )
                total += 1
            conn.commit()
            logger.info(f"auction_benchmark collected: {len(bench)} rows")
        else:
            errors.append("auction_benchmark: 无数据 (非竞价时段或源不可用)")

        # 2. 竞价快照 (对风向标标的批量查询; 无标的则跳过)
        if thscodes:
            snaps, _src = self._fetch("auction_snapshot", {"codes": ",".join(thscodes)})
            if snaps:
                for r in snaps:
                    code = r.get("ticker") or _code_from_thscode(r.get("thscode", ""))
                    if not code:
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO auction_snapshot "
                        "(date, code, name, auction_price, auction_pct, auction_volume, "
                        "auction_amount, auction_unmatched, auction_turnover_pct) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (input.date, code, r.get("name", ""),
                         _safe_float(r.get("auction_price")),
                         _safe_float(r.get("auction_pct")),
                         _safe_float(r.get("auction_volume")),
                         _safe_float(r.get("auction_amount")),
                         _safe_float(r.get("auction_unmatched")),
                         _safe_float(r.get("auction_turnover_pct"))),
                    )
                    total += 1
                conn.commit()
                logger.info(f"auction_snapshot collected: {len(snaps)} rows")
            else:
                errors.append("auction_snapshot: 无数据")

        status = "success" if not errors else ("partial" if total > 0 else "failed")
        return PipelineResult(
            name=self.name, date=input.date, status=status,
            records_written=total, errors=errors,
        )

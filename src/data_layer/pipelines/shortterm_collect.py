"""pipelines/shortterm_collect.py — DL-07 短线结构/情绪冷采集管线

采集不可回填的时序快照型数据 (用户预决策 #7): 错过当日即永久失去,
即使当前无消费者也必须立即开始积累。只做"冷采集 + 最低治理":
落表 + 来源记录 + 新鲜度检查 (DL-12 接入), 不建因子不进选股链路。

fuyao 端点 (DL-00 实测全部可用, 见 docs/source_benchmark_2026-08-18.md):
- limit-up-ladder      涨停天梯 (30日连板矩阵)  → limit_up_ladder (+_meta)
- limit-break-pool     炸板池                    → limit_break_pool
- hot-stock-list-history 历史热股榜              → hot_stock_history
- skyrocket-list       飙升榜                    → skyrocket_list
- anomaly-analysis-list 个股异动原因             → stock_anomaly_reasons
akshare 端点:
- stock_sector_fund_flow_rank 板块资金流 (不可回填) → sector_fund_flow

竞价快照 (auction_benchmark/auction_snapshot) 由 AuctionCollectPipeline
在 auction_parse (9:25) 采集, 不在本管线。

注意: fuyao 免费但仍有频控, 各端点串行采集勿并发打满。
"""

from __future__ import annotations

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.shortterm_collect")

# 本管线落库的全部表 (供新鲜度检查/读 API/测试引用)
COLLECT_TABLES = [
    "limit_up_ladder", "limit_up_ladder_meta", "limit_break_pool",
    "hot_stock_history", "skyrocket_list", "stock_anomaly_reasons",
    "sector_fund_flow",
]

# AUD-002 警示: 下面 DDL 的时间列默认是 **UTC** (CURRENT_TIMESTAMP /
# datetime('now')), 与全库 BJT 主流 (datetime('now','+8 hours')) 不同。
# 加日期过滤、时间窗查询或渲染给人看之前 **必须先转换**, 否则差 8 小时。
# 禁止只把这里的默认值改成 BJT —— 那会让新行 BJT、存量行 UTC 落进同一列,
# 从「一致的 UTC(安全)」退化成「同列混写(静默出错)」; 要改须连存量一起平移。
# 定谳与三路方案见 docs/audit/OPUS5_DEEP_AUDIT.md 的 AUD-002 段。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS limit_up_ladder (
    snapshot_date TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    board_level TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    board_num INTEGER,
    seal_nextday INTEGER,
    sign_level INTEGER,
    source TEXT DEFAULT 'fuyao',
    PRIMARY KEY (snapshot_date, trade_date, board_level, code)
);
CREATE TABLE IF NOT EXISTS limit_up_ladder_meta (
    snapshot_date TEXT PRIMARY KEY,
    window_length INTEGER,
    board_caps_json TEXT,
    date_list_json TEXT,
    source TEXT DEFAULT 'fuyao'
);
CREATE TABLE IF NOT EXISTS limit_break_pool (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    last_price REAL,
    change_pct REAL,
    open_times INTEGER,
    turnover_ratio REAL,
    turnover REAL,
    source TEXT DEFAULT 'fuyao',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS hot_stock_history (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    rank INTEGER,
    source TEXT DEFAULT 'fuyao',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS skyrocket_list (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    rank INTEGER,
    heat REAL,
    rank_change INTEGER,
    rank_trend TEXT,
    source TEXT DEFAULT 'fuyao',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS stock_anomaly_reasons (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    tag_name TEXT,
    analysis_content TEXT,
    keywords TEXT,
    source TEXT DEFAULT 'fuyao',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, code, tag_name)
);
CREATE TABLE IF NOT EXISTS sector_fund_flow (
    date TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    sector_type TEXT NOT NULL,
    change_pct REAL,
    main_net_amount REAL,
    main_net_ratio REAL,
    super_large_amount REAL,
    large_amount REAL,
    medium_amount REAL,
    small_amount REAL,
    top_stock TEXT,
    source TEXT DEFAULT 'akshare',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, sector_name, sector_type)
);
"""


def _code_from_thscode(thscode: str) -> str:
    """'300558.SZ' → '300558'; 无后缀原样返回。"""
    return thscode.split(".")[0] if thscode else ""


def _safe_float(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


class ShortTermCollectPipeline(BasePipeline):
    """短线结构/情绪冷采集 — 每日 15:30 后随 daily_data_pipeline 执行。"""

    name = "shortterm_collect"
    requires = []
    produces = COLLECT_TABLES

    def __init__(self, db=None, source_registry=None):
        self.db = db
        self.source_registry = source_registry

    def _fetch(self, data_type: str, params: dict) -> tuple[object, str]:
        """经 SourceRegistry 取数 (fuyao 优先), 返回 (data, source_name)。"""
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

    @classmethod
    def _snapshot_collectors(cls):
        """快照类六项采集器名单 (单一事实源 — 首轮 run 与 16:30 补采轮
        shortterm_recollect 共用, 防两处漂移)。

        刀2 (fix/anomaly-1630-recollect, 派单 sm-7c5089a73bd44eaf):
        anomaly-analysis-list 等端点是纯当日快照 (无日期参数), 上游发布
        可能晚于 15:44 首轮窗 (09-29 16:48 采得 200 行 vs 10-09 15:44
        空), 错过即永久丢 (DL-07)。补采轮只许跑这六项 — 主腿
        (kline/valuation/moneyflow/fuyao_financial) 绝不进 (TDD 集合
        断言钉死, 防后人加腿)。
        """
        return [
            ("limit_ladder", cls._collect_ladder),
            ("limit_break", cls._collect_limit_break),
            ("hot_stock_history", cls._collect_hot_history),
            ("skyrocket", cls._collect_skyrocket),
            ("anomaly", cls._collect_anomaly),
            ("sector_fund_flow", cls._collect_sector_flow),
        ]

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

        collectors = self._snapshot_collectors()
        for data_type, fn in collectors:
            try:
                # _snapshot_collectors 是 classmethod, 返回的是未绑定函数
                # (cls._collect_x) — 此处须显式传 self (刀2: 与补采轮
                # shortterm_recollect 的 fn(inner, conn, date) 同构)。
                n = fn(self, conn, input.date)
                if n > 0:
                    total += n
                    collected.append(f"{data_type}:{n}")
                else:
                    errors.append(f"{data_type}: 无数据 (源不可用或非交易日)")
            except Exception as e:
                errors.append(f"{data_type}: 采集异常 {e}")
            conn.commit()

        status = "success" if not errors else ("partial" if total > 0 else "failed")
        return PipelineResult(
            name=self.name, date=input.date, status=status,
            records_written=total, errors=errors,
            metadata={"collected": collected},
        )

    # --- 各采集项 ---

    def _collect_ladder(self, conn, date: str) -> int:
        ladder, _src = self._fetch("limit_ladder", {"date": date})
        if not isinstance(ladder, dict):
            return 0
        n = 0
        window = ladder.get("window") or {}
        import json
        conn.execute(
            "INSERT OR REPLACE INTO limit_up_ladder_meta "
            "(snapshot_date, window_length, board_caps_json, date_list_json) VALUES (?, ?, ?, ?)",
            (date, window.get("length"),
             json.dumps(window.get("board_caps") or {}, ensure_ascii=False),
             json.dumps(window.get("date_list") or [], ensure_ascii=False)),
        )
        n += 1
        for day_item in ladder.get("item") or []:
            trade_date = day_item.get("date", "")
            boards = day_item.get("boards") or {}
            for board_level, stocks in boards.items():
                for s in stocks or []:
                    code = s.get("ticker") or _code_from_thscode(s.get("thscode", ""))
                    if not code:
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO limit_up_ladder "
                        "(snapshot_date, trade_date, board_level, code, name, "
                        "board_num, seal_nextday, sign_level) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (date, trade_date, board_level, code, s.get("name", ""),
                         int(s.get("board_num") or 0),
                         s.get("seal_nextday"),
                         int(s.get("sign_level") or 0)),
                    )
                    n += 1
        logger.info(f"limit_ladder collected: {n} rows")
        return n

    def _collect_limit_break(self, conn, date: str) -> int:
        rows, _src = self._fetch("limit_break", {"date": date})
        if not rows:
            return 0
        n = 0
        for r in rows:
            code = r.get("ticker") or _code_from_thscode(r.get("thscode", ""))
            if not code:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO limit_break_pool "
                "(date, code, name, last_price, change_pct, open_times, "
                "turnover_ratio, turnover) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (date, code, r.get("name", ""),
                 _safe_float(r.get("last_price")),
                 _safe_float(r.get("price_change_ratio_pct")),
                 int(r.get("open_times") or 0),
                 _safe_float(r.get("turnover_ratio_pct")),
                 _safe_float(r.get("turnover"))),
            )
            n += 1
        logger.info(f"limit_break collected: {n} rows")
        return n

    def _collect_hot_history(self, conn, date: str) -> int:
        rows, _src = self._fetch("hot_stock_history", {"date": date})
        if not rows:
            return 0
        n = 0
        for r in rows:
            code = r.get("ticker") or _code_from_thscode(r.get("thscode", ""))
            if not code:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO hot_stock_history "
                "(date, code, name, rank) VALUES (?, ?, ?, ?)",
                (date, code, r.get("name", ""), int(r.get("rank") or 0)),
            )
            n += 1
        logger.info(f"hot_stock_history collected: {n} rows")
        return n

    def _collect_skyrocket(self, conn, date: str) -> int:
        rows, _src = self._fetch("skyrocket", {})
        if not rows:
            return 0
        n = 0
        for r in rows:
            code = r.get("ticker") or _code_from_thscode(r.get("thscode", ""))
            if not code:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO skyrocket_list "
                "(date, code, name, rank, heat, rank_change, rank_trend) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (date, code, r.get("name", ""), int(r.get("rank") or 0),
                 _safe_float(r.get("heat")), int(r.get("rank_change") or 0),
                 r.get("rank_trend", "")),
            )
            n += 1
        logger.info(f"skyrocket collected: {n} rows")
        return n

    def _collect_anomaly(self, conn, date: str) -> int:
        rows, _src = self._fetch("anomaly", {})
        if not rows:
            return 0
        n = 0
        for r in rows:
            # anomaly-analysis-list 无 ticker 字段, 从 thscode 派生 code
            code = r.get("ticker") or _code_from_thscode(r.get("thscode", ""))
            if not code:
                continue
            keywords = r.get("keyword_list") or []
            conn.execute(
                "INSERT OR REPLACE INTO stock_anomaly_reasons "
                "(date, code, name, tag_name, analysis_content, keywords) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (date, code, r.get("stock_name") or r.get("name", ""),
                 r.get("tag_name", ""), r.get("analysis_content", ""),
                 ",".join(keywords) if isinstance(keywords, list) else str(keywords)),
            )
            n += 1
        logger.info(f"anomaly collected: {n} rows")
        return n

    def _collect_sector_flow(self, conn, date: str) -> int:
        rows, _src = self._fetch("sector_fund_flow", {"date": date})
        if not rows:
            return 0
        n = 0
        for r in rows:
            name = r.get("sector_name", "")
            stype = r.get("sector_type", "")
            if not name:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO sector_fund_flow "
                "(date, sector_name, sector_type, change_pct, main_net_amount, "
                "main_net_ratio, super_large_amount, large_amount, medium_amount, "
                "small_amount, top_stock) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (date, name, stype,
                 _safe_float(r.get("change_pct")),
                 _safe_float(r.get("main_net_amount")),
                 _safe_float(r.get("main_net_ratio")),
                 _safe_float(r.get("super_large_amount")),
                 _safe_float(r.get("large_amount")),
                 _safe_float(r.get("medium_amount")),
                 _safe_float(r.get("small_amount")),
                 r.get("top_stock", "")),
            )
            n += 1
        logger.info(f"sector_fund_flow collected: {n} rows")
        return n

"""DataLayer 读接口 — snapshot + snapshot_intraday"""

from datetime import datetime, timedelta

from common.database import Database
from common.logging import get_logger
from common.utils import validate_identifier

# fwd_return 单一事实源: 全仓 horizon->列名映射只此一份。
# 消费方: loader 三处读 API/回填、ops 回填调度、IC 反馈、GO-F 历史回补脚本、
# DDL 脚本 (add_fwd_return_10d_column.py)。禁止在别处复制字面量 (断裂五形第五形)。
FWD_HORIZONS = (1, 3, 5, 10, 20)
FWD_HORIZON_COLS = {
    1: "fwd_return_1d",
    3: "fwd_return_3d",
    5: "fwd_return_5d",
    10: "fwd_return_10d",
    20: "fwd_return_20d",
}
from data_layer._models import (
    AnalystForecast,
    ConceptData,
    DataSnapshot,
    Financial,
    HistoryWindow,
    HotStock,
    IntradaySnapshot,
    Kline,
    LhbData,
    LimitUpDown,
    MarketState,
    Moneyflow,
    NewsItem,
    ReadinessReport,
    RealTimeQuote,
    SentimentData,
    StockInfo,
    Valuation,
)
from data_layer.health.readiness import DataReadiness

logger = get_logger("data")


def _norm_fin_date(value) -> str:
    s = str(value or "")
    compact = s.replace("-", "")
    if len(compact) == 8 and compact.isdigit():
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    return s


class DataLoader:
    def __init__(self, db: Database):
        self.db = db
        self._readiness = DataReadiness(db)
        # DL-04: get_risky_codes 当日缓存 (fail-closed 兜底)
        self._risky_cache: tuple[str, set[str]] | None = None
        # 区间存储读侧: concept_membership 是否存在的缓存 (None=未探测)。
        # 生产库建表需另行 GO, 未建时 get_concept_data 走 concept_daily 同日兜底。
        self._interval_tables_cache: bool | None = None
        # DL-44: baostock 权威停牌判据 (延迟初始化 + 按日缓存)
        self._bs_adapter = None
        self._bs_adapter_init = False
        self._trade_status_cache: tuple[str, dict[str, bool]] | None = None

    def snapshot(self, date: str) -> DataSnapshot:
        conn = self.db.get_connection()
        readiness = self._readiness.check(date)
        market = self._get_market_state(conn, date)
        klines = self._get_klines(conn, date)
        financials = self._get_financials(conn, date)
        valuations = self._get_valuations(conn, date)
        moneyflow = self._get_moneyflow(conn, date)
        news = self._get_news(conn, date)
        limit_up_down = self._get_limit_up_down(conn, date)
        return DataSnapshot(
            date=date,
            market=market,
            klines=klines,
            financials=financials,
            valuations=valuations,
            moneyflow=moneyflow,
            news=news,
            limit_up_down=limit_up_down,
            readiness=readiness,
        )

    def snapshot_intraday(self, timestamp: str | None = None, source_registry=None) -> IntradaySnapshot:
        """盘中快照 — 热股/实时行情/涨跌停经 SourceRegistry 取数 (DL-08)。

        旧实现硬编码 FuyaoClient() 且裸 `except: pass` 吞错 (F-10①)。改为:
        - 各类数据走 source_registry (hot_stock/realtime_quote/limit_up/limit_down),
          源不可用/失败 → 记日志 + 该字段降级为空, 不再静默吞错;
        - 行情/涨停止用各适配器归一化后的统一契约 (code/price/change_pct/...)。
        """
        from common.utils import bj_date, bj_now
        ts = timestamp or bj_now()
        date = bj_date()
        conn = self.db.get_connection()
        klines = self._get_klines(conn, date)
        market = self._get_market_state(conn, date)

        hot_stocks: list[HotStock] = []
        realtime_quotes: list[RealTimeQuote] = []
        limit_up_down: list[LimitUpDown] = []
        minute_data = []
        anomalies = []
        news = []

        # 热股榜 (fuyao 唯一源)
        hot_codes: list[str] = []
        if source_registry:
            try:
                for src in source_registry.get_sources_for("hot_stock"):
                    hot_data = src.get_data("hot_stock", {})
                    if hot_data:
                        for i, h in enumerate(hot_data[:10]):
                            code = h.get("ticker") or (h.get("thscode", "") or "").split(".")[0]
                            if not code:
                                continue
                            hot_codes.append(code)
                            hot_stocks.append(HotStock(
                                code=code, name=h.get("name", ""),
                                rank=int(h.get("rank") or (i + 1)),
                                heat=float(h.get("heat") or 0),
                            ))
                        break
            except Exception as e:
                logger.warning(f"snapshot_intraday hot_stock 取数失败, 降级为空: {e}")

        # 实时行情 (对热股标的; 优先批量源 gtimg, 逐只兜底)
        if source_registry and hot_codes:
            try:
                for q in self._fetch_realtime_quotes(hot_codes, source_registry):
                    realtime_quotes.append(RealTimeQuote(
                        code=q.get("code", ""), name=q.get("name", ""),
                        current=float(q.get("price", 0) or 0),
                        open=float(q.get("open", 0) or 0),
                        high=float(q.get("high", 0) or 0),
                        low=float(q.get("low", 0) or 0),
                        volume=float(q.get("volume", 0) or 0),
                        amount=float(q.get("amount", 0) or 0),
                        change_pct=float(q.get("change_pct", 0) or 0),
                    ))
            except Exception as e:
                logger.warning(f"snapshot_intraday realtime_quote 取数失败, 降级为空: {e}")

        # 涨停/跌停池 (fuyao 主 / akshare 备, 均已归一化)
        if source_registry:
            type_label = {"limit_up": "涨停", "limit_down": "跌停"}
            for data_type in ("limit_up", "limit_down"):
                try:
                    for src in source_registry.get_sources_for(data_type):
                        rows = src.get_data(data_type, {"date": date})
                        if rows:
                            for item in rows[:20]:
                                limit_up_down.append(LimitUpDown(
                                    code=item.get("code", ""), name=item.get("name", ""),
                                    date=date, limit_type=type_label[data_type],
                                    price=float(item.get("price", 0) or 0),
                                    change_pct=float(item.get("change_pct", 0) or 0),
                                ))
                            break
                except Exception as e:
                    logger.warning(f"snapshot_intraday {data_type} 取数失败, 降级为空: {e}")

        # 通过 SourceRegistry 获取分钟级数据（EasyTDX / 未来 Fuyao）
        if source_registry:
            try:
                minute_sources = source_registry.get_sources_for("minute_data")
                for code in (hot_stocks[:5] if hot_stocks else []):
                    for src in minute_sources:
                        bars = src.get_data("minute_data", {"code": code.code, "count": 120})
                        if bars:
                            minute_data.extend(bars[:120])
                            break
            except Exception as e:
                logger.debug(f"snapshot_intraday minute_data 取数失败: {e}")

        return IntradaySnapshot(
            timestamp=ts,
            realtime_quotes=realtime_quotes,
            minute_data=minute_data if minute_data else None,
            klines_today=klines,
            limit_up_down=limit_up_down,
            anomalies=anomalies,
            hot_stocks=hot_stocks,
            news=news,
            capital_flow=None,
            market_index=market.indices if hasattr(market, 'indices') else [],
        )

    def _fetch_realtime_quotes(self, codes: list[str], source_registry) -> list[dict]:
        """经 SourceRegistry 批量取实时行情, 返回统一契约 dict 列表 (DL-08)。

        优先 realtime_quote_batch (gtimg, 一次 HTTP); 无批量结果时逐只
        realtime_quote 兜底。各适配器已归一化为 code/name/price/change_pct/...。
        """
        if not codes:
            return []
        # 批量源优先
        for src in source_registry.get_sources_for("realtime_quote_batch"):
            try:
                rows = src.get_data("realtime_quote_batch", {"codes": list(codes)})
                if rows:
                    return rows
            except Exception as e:
                logger.debug(f"realtime_quote_batch {getattr(src, 'name', '?')} failed: {e}")
        # 逐只兜底 (gtimg 单只 dict / fuyao 批量 list, 同时传 code+codes 兼容两者)
        results: list[dict] = []
        got: set[str] = set()
        for code in codes:
            if code in got:
                continue
            for src in source_registry.get_sources_for("realtime_quote"):
                try:
                    q = src.get_data("realtime_quote", {"code": code, "codes": code})
                    rows = q if isinstance(q, list) else ([q] if q else [])
                    for r in rows:
                        c = r.get("code", "")
                        if c and c not in got:
                            results.append(r)
                            got.add(c)
                    if code in got:
                        break
                except Exception as e:
                    logger.debug(f"realtime_quote {getattr(src, 'name', '?')} {code} failed: {e}")
        return results

    def check_readiness(self, date: str) -> ReadinessReport:
        return self._readiness.check(date)

    def _get_market_state(self, conn, date: str) -> MarketState:
        try:
            rows = conn.execute(
                "SELECT change_pct FROM daily_klines WHERE date = ?", (date,)
            ).fetchall()
            if not rows:
                return MarketState(state="未知", indices=[])
            changes = [r["change_pct"] or 0 for r in rows]
            up_count = sum(1 for c in changes if c > 0)
            down_count = sum(1 for c in changes if c < 0)
            avg_change = sum(changes) / len(changes) if changes else 0
            if avg_change > 2:
                state = "bull"
            elif avg_change < -2:
                state = "bear"
            else:
                state = "neutral"
            return MarketState(
                state=state, indices=[],
                up_count=up_count, down_count=down_count,
                market_temperature=round(avg_change, 2),
            )
        except Exception:
            return MarketState(state="未知", indices=[])

    def _get_klines(self, conn, date: str) -> list[Kline]:
        try:
            rows = conn.execute(
                "SELECT * FROM daily_klines WHERE date = ?", (date,)
            ).fetchall()
            return [Kline(
                code=r["code"], date=r["date"],
                open=r["open"] or 0, high=r["high"] or 0,
                low=r["low"] or 0, close=r["close"] or 0,
                volume=r["volume"] or 0, amount=r["amount"] or 0,
                pre_close=r["pre_close"] or 0,
                change_pct=r["change_pct"] or 0,
                turnover_rate=r["turnover_rate"] or 0,
            ) for r in rows]
        except Exception:
            return []

    def _get_financials(self, conn, date: str) -> list[Financial]:
        try:
            ym = date[:7].replace("-", "")
            rows = conn.execute(
                "SELECT * FROM stock_financials WHERE date = ? OR date = ?", (date, ym)
            ).fetchall()
            return [Financial(
                code=r["code"], date=_norm_fin_date(r["date"]),
                eps=r["eps"] or 0, roe=r["roe"] or 0,
                revenue=r["revenue"] or 0, profit=r["profit"] or 0,
                total_assets=r["total_assets"] or 0,
                total_liabilities=r["total_liabilities"] or 0,
                net_assets=r["net_assets"] or 0,
                operating_cash_flow=r["operating_cash_flow"] or 0,
                gross_margin=r["gross_margin"] or 0,
                net_margin=r["net_margin"] or 0,
            ) for r in rows]
        except Exception:
            return []

    def _get_valuations(self, conn, date: str) -> list[Valuation]:
        try:
            rows = conn.execute(
                "SELECT * FROM stock_valuation WHERE date = ?", (date,)
            ).fetchall()
            result = []
            for r in rows:
                rk = set(r.keys())
                result.append(Valuation(
                    code=r["code"], date=r["date"],
                    pe_ttm=r["pe_ttm"] if "pe_ttm" in rk else None,
                    pb_mrq=r["pb_mrq"] if "pb_mrq" in rk else None,
                    ps_ttm=r["ps_ttm"] if "ps_ttm" in rk else None,
                    market_cap=r["market_cap"] if "market_cap" in rk else None,
                    total_shares=r["total_shares"] if "total_shares" in rk else None,
                    dividend_yield=r["dividend_yield"] if "dividend_yield" in rk else None,
                ))
            return result
        except Exception:
            return []

    def _get_moneyflow(self, conn, date: str) -> list[Moneyflow]:
        try:
            date_fmt = date.replace("-", "")
            rows = conn.execute(
                "SELECT * FROM stock_moneyflow WHERE date = ? OR date = ?",
                (date, date_fmt),
            ).fetchall()
            return [Moneyflow(
                code=r["code"], date=r["date"],
                main_net_inflow=r["main_net_inflow"] or 0,
                retail_net_inflow=r["retail_net_inflow"] or 0,
                main_inflow=r["main_inflow"] or 0,
                main_outflow=r["main_outflow"] or 0,
            ) for r in rows]
        except Exception:
            return []

    def _get_news(self, conn, date: str) -> list[NewsItem]:
        try:
            rows = conn.execute(
                "SELECT * FROM stock_news WHERE date LIKE ?", (date + "%",)
            ).fetchall()
            result = []
            for r in rows:
                rk = set(r.keys())
                result.append(NewsItem(
                    stock_code=r["code"] if "code" in rk else "",
                    title=r["title"] if "title" in rk else "",
                    source=r["source"] if "source" in rk else "",
                    date=r["date"],
                    sentiment_label=r["sentiment"] if "sentiment" in rk else "neutral",
                    sentiment_score=r["sentiment_score"] if "sentiment_score" in rk else 50.0,
                    content=r["summary"] if "summary" in rk else "",
                ))
            return result
        except Exception:
            return []

    def _get_limit_up_down(self, conn, date: str) -> list[LimitUpDown]:
        try:
            rows = conn.execute(
                "SELECT * FROM stock_limit_up_down WHERE date = ?", (date,)
            ).fetchall()
            result = []
            for r in rows:
                rk = set(r.keys())
                result.append(LimitUpDown(
                    code=r["code"], name=r["name"] if "name" in rk else "",
                    date=r["date"], limit_type=r["limit_type"] if "limit_type" in rk else "",
                    price=r["price"] if "price" in rk else 0.0,
                    change_pct=r["change_pct"] if "change_pct" in rk else 0.0,
                    涨停次数=r["count"] if "count" in rk else 0,
                ))
            return result
        except Exception:
            return []

    def get_history(self, code: str, end_date: str, window: int) -> HistoryWindow:
        """返回某只股票的历史数据窗口（klines, financials, valuations）"""
        conn = self.db.get_connection()

        d = datetime.strptime(end_date, "%Y-%m-%d")
        start_date = (d - timedelta(days=window * 2)).strftime("%Y-%m-%d")

        klines = self._get_klines_range(conn, code, start_date, end_date)

        financial_start = (d - timedelta(days=365)).strftime("%Y-%m-%d")
        financials = self._get_financials_range(conn, code, financial_start, end_date)

        valuation_start = (d - timedelta(days=30)).strftime("%Y-%m-%d")
        valuations = self._get_valuations_range(conn, code, valuation_start, end_date)

        moneyflow = self._get_moneyflow_range(conn, code, start_date, end_date)

        return HistoryWindow(
            code=code,
            end_date=end_date,
            window=window,
            klines=klines,
            financials=financials,
            valuations=valuations,
            moneyflow=moneyflow,
        )

    def forward_return(self, code: str, date: str, horizon: int = 5) -> float | None:
        """获取某只股票的未来N日收益率 (单位: 百分点, 与 factor_daily 存储一致)

        DL-24 语义修复: 优先读 factor_daily; 兜底从 daily_klines 现算
        "该日后第 N 个交易日的收盘"。未来未兑现 (无后续交易日数据) → None,
        不再返回假 0 (旧实现 date<=窗口 ORDER BY date DESC 在最新日期会命中
        当前行自身, 恒产 0, 是 IC 环全 0 的上游根因)。
        """
        conn = self.db.get_connection()

        horizon_map = FWD_HORIZON_COLS
        col = horizon_map.get(horizon)

        if col:
            if not validate_identifier(col, "column"):
                return None
            try:
                row = conn.execute(
                    f"SELECT {col} FROM factor_daily WHERE code = ? AND date = ?",
                    (code, date),
                ).fetchone()
                if row and row[col] is not None:
                    return row[col]
            except Exception:
                pass  # factor_daily 缺失 → 落到 kline 现算

        current = conn.execute(
            "SELECT close FROM daily_klines WHERE code = ? AND date = ?",
            (code, date),
        ).fetchone()
        if not current or not current["close"]:
            return None

        # 该日后第 horizon 个交易日 (OFFSET 跳过 horizon-1 行), 严格在未来
        future = conn.execute(
            "SELECT close FROM daily_klines WHERE code = ? AND date > ? "
            "ORDER BY date LIMIT 1 OFFSET ?",
            (code, date, horizon - 1),
        ).fetchone()
        if not future or not future["close"]:
            return None  # 未来未兑现 → None, 不造假 0

        return (future["close"] - current["close"]) / current["close"] * 100

    def fwd_returns_cross_section(self, date: str, horizon: int = 5) -> dict:
        """DL-24: 某日全市场前向收益截面 {code: 收益率(百分点)}。

        统一评估读 API, 供策略 IC 环 / 回测 agent_eval / ops evaluate 消费。
        只返回 factor_daily 中已兑现 (非 NULL) 的值; 表缺失返回 {}。
        """
        horizon_map = FWD_HORIZON_COLS
        col = horizon_map.get(horizon)
        if not col or not validate_identifier(col, "column"):
            return {}
        conn = self.db.get_connection()
        try:
            rows = conn.execute(
                f"SELECT code, {col} AS fwd FROM factor_daily "
                f"WHERE date = ? AND {col} IS NOT NULL",
                (date,),
            ).fetchall()
        except Exception:
            return {}
        return {r["code"]: r["fwd"] for r in rows}

    @staticmethod
    def _table_exists(conn, table: str) -> bool:
        try:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone() is not None
        except Exception:
            return False

    def backfill_fwd_returns(self, target_date: str, horizons: tuple = FWD_HORIZONS) -> dict:
        """DL-24: 为指定日期回填前向收益到 factor_daily (每日调度入口)。

        对每个 horizon 从 daily_klines 现算 "target_date 后第 N 个交易日收盘",
        仅写 factor_daily 中为 NULL 的行; 幂等。返回 {horizon: 填充行数}。
        未来未兑现的行保持 NULL (等后续交易日到来再由调度补上)。
        """
        conn = self.db.get_connection()
        horizon_map = FWD_HORIZON_COLS
        # 缺列防御 (fwd_10d 写好待 GO): 列未建期间每日管道照常跑已有周期。
        # 缺列的 horizon 跳过并留痕, 不静默不炸管道 (铁律 #12)。
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(factor_daily)").fetchall()} \
            if self._table_exists(conn, "factor_daily") else set()
        filled = {}
        for h in horizons:
            col = horizon_map.get(h)
            if not col or not validate_identifier(col, "column"):
                continue
            if existing_cols and col not in existing_cols:
                filled[h] = -1  # 约定: -1 = 列缺失被跳过 (调用方记 errors)
                continue
            conn.execute("DROP TABLE IF EXISTS temp_fwd_target")
            conn.execute("""
                CREATE TEMP TABLE temp_fwd_target AS
                SELECT code, date, close AS cur_close,
                       (SELECT close FROM daily_klines
                        WHERE code = k.code AND date > k.date
                        ORDER BY date LIMIT 1 OFFSET ?) AS fut_close
                FROM daily_klines k
                WHERE date = ? AND close > 0
            """, (h - 1, target_date))
            try:
                cur = conn.execute(f"""
                    UPDATE factor_daily
                    SET {col} = (SELECT (f.fut_close - f.cur_close) / f.cur_close * 100
                                 FROM temp_fwd_target f
                                 WHERE f.code = factor_daily.code
                                   AND f.date = factor_daily.date)
                    WHERE date = ? AND {col} IS NULL
                      AND EXISTS (SELECT 1 FROM temp_fwd_target f
                                  WHERE f.code = factor_daily.code
                                    AND f.date = factor_daily.date
                                    AND f.fut_close IS NOT NULL AND f.fut_close > 0)
                """, (target_date,))
                filled[h] = cur.rowcount
            finally:
                conn.execute("DROP TABLE IF EXISTS temp_fwd_target")
        conn.commit()
        return filled

    def get_sector(self, code: str) -> str:
        try:
            from data_layer.repos.stock import StockRepo
            repo = StockRepo(self.db)
            info = repo.get_by_code(code)
            return info.get("industry", "") if info else ""
        except Exception:
            return ""

    def get_stock_info(self, code: str) -> StockInfo | None:
        try:
            from data_layer.repos.stock import StockRepo
            repo = StockRepo(self.db)
            row = repo.get_by_code(code)
            if not row:
                return None
            return StockInfo(
                code=row.get("code", code),
                name=row.get("name", ""),
                industry=row.get("industry", ""),
                market=row.get("market", "SZ"),
                list_date=row.get("list_date", ""),
                is_active=bool(row.get("is_active", 1)),
            )
        except Exception:
            return None

    def is_st_stock(self, code: str) -> bool:
        """ST 判定: 通过股票名称判断(ST/*ST 前缀), 而非 code 前缀。
        真实 A 股 ST 股 code 为数字开头(如 000430), code.startswith('ST') 永远 False。
        """
        try:
            conn = self.db.get_connection()
            row = conn.execute(
                "SELECT name FROM stock_info WHERE code = ?",
                (code,),
            ).fetchone()
            if not row:
                return False
            name = row[0] or ""
            return name.startswith("ST") or name.startswith("*ST") or "ST" in name[:3]
        except Exception:
            return False

    def get_risky_codes(self) -> set[str] | None:
        """批量取 ST/退市/停牌风险码 (一次查询, 供选股 L1 安全闸使用)。

        判定口径与 is_st_stock 一致 (按名称而非 code 前缀), 另加:
        - 名称含 "退" (退市整理板, 如 立方退)
        - is_active=0 (已退市)

        DL-04 fail-closed: 查询失败返回 None (区别于空集), 调用方必须中止选股并告警,
        不得拿空集放行。当日缓存兜底: 查询失败但当日有成功结果时用缓存+告警。
        """
        from common.utils import bj_date
        today = bj_date()
        try:
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT code FROM stock_info "
                "WHERE name LIKE '%ST%' OR name LIKE '%退%' OR is_active = 0"
            ).fetchall()
            codes = {r[0] for r in rows if r and r[0]}
            self._risky_cache = (today, codes)
            return codes
        except Exception as e:
            if self._risky_cache is not None and self._risky_cache[0] == today:
                logger.warning(
                    f"get_risky_codes 查询失败, 使用当日缓存兜底 ({len(self._risky_cache[1])} 码): {e}")
                return self._risky_cache[1]
            logger.error(f"get_risky_codes 查询失败且无当日缓存 — 安全闸 fail-closed: {e}")
            return None

    def _get_baostock_adapter(self):
        """DL-44: 延迟获取 baostock 适配器 (is_suspended 权威判据)。"""
        if not self._bs_adapter_init:
            self._bs_adapter_init = True
            try:
                from data_layer.sources.baostock_source import BaoStockSourceAdapter
                adapter = BaoStockSourceAdapter()
                if adapter.is_available():
                    self._bs_adapter = adapter
            except Exception:  # noqa: BLE001
                pass
        return self._bs_adapter

    def is_suspended(self, code: str, date: str) -> bool:
        """DL-44: 停牌判定 — baostock tradestatus 权威判据优先, 推断判据降级。

        优先级:
        1. baostock tradestatus (权威): tradestatus=0 → 停牌, =1 → 未停牌
           (按日批量缓存, 单次调用覆盖全市场, 避免逐股网络调用)
        2. 推断判据 (baostock 不可用/无数据时降级, 原 DL-17 逻辑):
           当日kline缺失 + 个股在市 + 市场当日有行情

        DL-17 原注释保留:
        旧判据"无 kline 行 = 停牌" (F-12) 误判三类场景, 现组合判定:
        - 当日有 kline → 未停牌
        - 退市/停牌元数据缺失外的不在市 (is_active=0 或 delist_date 已过) → 非停牌
        - 新股 (list_date 晚于当日) → 非停牌 (尚未上市)
        - 市场当日无行情 (非交易日/数据缺失) → 不判停牌 (无法区分)
        - 其余: 在市 + 市场开市 + 该股缺 kline → 停牌
        """
        # DL-44: 尝试 baostock 权威判据 (按日批量缓存)
        bs = self._get_baostock_adapter()
        if bs is not None:
            try:
                if (self._trade_status_cache is None
                        or self._trade_status_cache[0] != date):
                    status_map = bs.get_daily_trade_status(date)
                    if status_map is not None:
                        self._trade_status_cache = (date, status_map)
                if (self._trade_status_cache is not None
                        and self._trade_status_cache[0] == date):
                    plain_code = code.split(".", 1)[-1]
                    if plain_code in self._trade_status_cache[1]:
                        # tradestatus=1 → 正常交易 → 未停牌
                        return not self._trade_status_cache[1][plain_code]
            except Exception:  # noqa: BLE001 — baostock 任何异常都降级到推断判据
                pass

        # 推断判据 (原 DL-17 逻辑, baostock 不可用时的降级路径)
        try:
            conn = self.db.get_connection()
            # 1. 当日有 kline → 必然未停牌
            row = conn.execute(
                "SELECT 1 FROM daily_klines WHERE code = ? AND date = ? AND close IS NOT NULL",
                (code, date),
            ).fetchone()
            if row is not None:
                return False
            # 2. 个股状态: 未知/退市/新股 → 非停牌
            info = conn.execute(
                "SELECT list_date, is_active, delist_date FROM stock_info WHERE code = ?",
                (code,),
            ).fetchone()
            if info is None:
                return False
            if not info["is_active"]:
                return False
            date_fmt = date.replace("-", "")
            list_date = info["list_date"] or ""
            if list_date and list_date.replace("-", "") > date_fmt:
                return False  # 新股: 当日尚未上市
            delist_date = info["delist_date"] or ""
            if delist_date and delist_date.replace("-", "") <= date_fmt:
                return False  # 当日已退市
            # 3. 市场当日须有行情, 否则属数据缺失/非交易日, 不判停牌
            mkt = conn.execute(
                "SELECT 1 FROM daily_klines WHERE date = ? LIMIT 1", (date,)
            ).fetchone()
            if mkt is None:
                return False
            return True
        except Exception:
            return False

    def get_sentiment_data(self, date: str) -> SentimentData | None:
        try:
            conn = self.db.get_connection()
            row = conn.execute("SELECT * FROM market_sentiment WHERE date = ?", (date,)).fetchone()
            if not row:
                return None
            return SentimentData(
                date=row["date"],
                market_temp=row["market_temp"] or 0.0,
                limit_up_count=row["limit_up_count"] or 0,
                limit_down_count=row["limit_down_count"] or 0,
                up_count=row["up_count"] or 0,
                down_count=row["down_count"] or 0,
                zt_count=row["zt_count"] or 0,
                dt_count=row["dt_count"] or 0,
            )
        except Exception:
            return None

    def _interval_tables_present(self) -> bool:
        """区间存储两表 (concept_membership + plate_daily_rate) 是否已建。

        生产库建表需另行 GO (统筹 sm-337ab3642f7f486f), 未建时读侧走
        concept_daily 同日兜底。探测结果缓存 (表只会从无到有, 不会消失)。
        """
        if self._interval_tables_cache is None:
            try:
                conn = self.db.get_connection()
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('concept_membership', 'plate_daily_rate')"
                ).fetchall()}
                self._interval_tables_cache = (
                    "concept_membership" in names and "plate_daily_rate" in names)
            except Exception:
                self._interval_tables_cache = False
        return self._interval_tables_cache

    def get_concept_data(self, code: str, date: str) -> list[ConceptData]:
        """严格同日 PIT 概念数据 (区间存储读侧, 统筹 sm-337ab3642f7f486f)。

        旧实现 `date <= ? ORDER BY date DESC LIMIT ?` 跨日取最近 10 行 →
        concept_count 数的是"跨日变化的名字个数"而非当日真概念 (假活, DL-66)。
        现改为**严格同日**: 只返回查询日当天在板的概念。

        - 区间表已建 → concept_membership 按 time_in/time_out 时点判定,
          change_pct 取 plate_daily_rate 当日 rate (无则 NULL→0.0, 与旧模型一致);
        - 区间表未建 (生产未 GO) → concept_daily 同日兜底 (date == 查询日)。

        """
        try:
            conn = self.db.get_connection()
            if self._interval_tables_present():
                rows = conn.execute(
                    "SELECT m.concept_name AS concept_name, r.rate AS change_pct "
                    "FROM concept_membership m "
                    "LEFT JOIN plate_daily_rate r "
                    "  ON r.plate_code = m.plate_code AND r.date = ? "
                    "WHERE m.code = ? AND m.time_in <= ? "
                    "  AND (m.time_out IS NULL OR m.time_out > ?)",
                    (date, code, date, date),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM concept_daily WHERE code = ? AND date = ?",
                    (code, date),
                ).fetchall()
            # DL-60: sqlite3.Row 无 .get() — dict(r) 后 .get, 保留缺列默认语义。
            return [
                ConceptData(
                    code=code, date=date,
                    concept_name=d.get("concept_name") or "",
                    change_pct=d.get("change_pct") or 0,
                )
                for d in (dict(r) for r in rows)
            ]
        except Exception:
            return []

    def get_lhb_data(self, code: str, date: str, limit: int = 10) -> list[LhbData]:
        try:
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT * FROM lhb_daily WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT ?",
                (code, date, limit),
            ).fetchall()
            # DL-60: 同 get_concept_data — dict(r) 后 .get, 修 Row.get AttributeError。
            return [
                LhbData(
                    code=d["code"], date=d["date"],
                    net_buy_amount=d.get("net_buy_amount") or 0,
                    drama_score=d.get("drama_score") or 0,
                )
                for d in (dict(r) for r in rows)
            ]
        except Exception:
            return []

    def get_analyst_forecasts(self, code: str, date: str, limit: int = 5) -> list[AnalystForecast]:
        try:
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT * FROM analyst_forecasts WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT ?",
                (code, date, limit),
            ).fetchall()
            # DL-60: 同上 — dict(r) 后 .get, 修 Row.get AttributeError。
            return [
                AnalystForecast(
                    code=d["code"], date=d["date"],
                    consensus_score=d.get("consensus_score") or 0,
                    target_price=d.get("target_price") or 0,
                    rating=d.get("rating") or "",
                )
                for d in (dict(r) for r in rows)
            ]
        except Exception:
            return []

    # === DL-07: 不可回填型情绪/短线结构数据读 API (冷采集, 暂供未来消费, 不接策略) ===
    # 表由 shortterm_collect/auction_collect 管线首跑时创建; 表不存在返回 []。

    def _query_dicts(self, sql: str, params: tuple = ()) -> list[dict]:
        try:
            conn = self.db.get_connection()
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_limit_ladder(self, date: str) -> dict:
        """涨停天梯 (30日连板矩阵): {"meta": {...}, "days": [{date, boards: {level: [stocks]}}]}"""
        meta_rows = self._query_dicts(
            "SELECT * FROM limit_up_ladder_meta WHERE snapshot_date = ?", (date,))
        rows = self._query_dicts(
            "SELECT * FROM limit_up_ladder WHERE snapshot_date = ? ORDER BY trade_date DESC",
            (date,))
        days: dict[str, dict[str, list[dict]]] = {}
        for r in rows:
            days.setdefault(r["trade_date"], {}).setdefault(r["board_level"], []).append(r)
        return {
            "meta": meta_rows[0] if meta_rows else None,
            "days": [{"date": d, "boards": b} for d, b in days.items()],
        }

    def get_limit_break_pool(self, date: str) -> list[dict]:
        """炸板池"""
        return self._query_dicts(
            "SELECT * FROM limit_break_pool WHERE date = ? ORDER BY turnover DESC", (date,))

    def get_hot_stock_history(self, date: str) -> list[dict]:
        """历史热股榜"""
        return self._query_dicts(
            "SELECT * FROM hot_stock_history WHERE date = ? ORDER BY rank", (date,))

    def get_skyrocket_list(self, date: str) -> list[dict]:
        """飙升榜"""
        return self._query_dicts(
            "SELECT * FROM skyrocket_list WHERE date = ? ORDER BY rank", (date,))

    def get_anomaly_reasons(self, date: str, code: str | None = None) -> list[dict]:
        """个股异动原因 (可按 code 过滤)"""
        if code:
            return self._query_dicts(
                "SELECT * FROM stock_anomaly_reasons WHERE date = ? AND code = ?",
                (date, code))
        return self._query_dicts(
            "SELECT * FROM stock_anomaly_reasons WHERE date = ?", (date,))

    def get_sector_fund_flow(self, date: str, sector_type: str | None = None) -> list[dict]:
        """板块资金流 (可按 industry/concept 过滤)"""
        if sector_type:
            return self._query_dicts(
                "SELECT * FROM sector_fund_flow WHERE date = ? AND sector_type = ? "
                "ORDER BY main_net_amount DESC", (date, sector_type))
        return self._query_dicts(
            "SELECT * FROM sector_fund_flow WHERE date = ? ORDER BY main_net_amount DESC",
            (date,))

    def get_auction_data(self, date: str) -> dict:
        """集合竞价数据: {"benchmark": [...], "snapshot": [...]}"""
        return {
            "benchmark": self._query_dicts(
                "SELECT * FROM auction_benchmark WHERE date = ? ORDER BY auction_pct DESC",
                (date,)),
            "snapshot": self._query_dicts(
                "SELECT * FROM auction_snapshot WHERE date = ?", (date,)),
        }

    def snapshot_range(self, start_date: str, end_date: str) -> list[DataSnapshot]:
        """返回日期范围内的所有数据"""
        snapshots = []
        current = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            try:
                snapshot = self.snapshot(date_str)
                snapshots.append(snapshot)
            except Exception as e:
                logger.warning(f"Failed to get snapshot for {date_str}: {e}")

            current += timedelta(days=1)

        return snapshots

    def _get_klines_range(self, conn, code: str, start_date: str, end_date: str) -> list[Kline]:
        try:
            rows = conn.execute(
                "SELECT * FROM daily_klines WHERE code = ? AND date BETWEEN ? AND ? ORDER BY date",
                (code, start_date, end_date),
            ).fetchall()
            return [Kline(
                code=r["code"], date=r["date"],
                open=r["open"] or 0, high=r["high"] or 0,
                low=r["low"] or 0, close=r["close"] or 0,
                volume=r["volume"] or 0, amount=r["amount"] or 0,
                pre_close=r["pre_close"] or 0,
                change_pct=r["change_pct"] or 0,
                turnover_rate=r["turnover_rate"] or 0,
            ) for r in rows]
        except Exception:
            return []

    @staticmethod
    def _normalize_financial_date(date_str: str) -> str:
        """归一化财务日期为可比较的 ISO YYYY-MM-DD。

        兼容 compact "202604"(Q2 2026) → 用季度末 "2026-06-30" 表达，
        与 ISO 报告期末日 "2026-06-30" 对齐。
        """
        s = (date_str or "").strip()
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return s
        if len(s) == 6 and s.isdigit():
            year = int(s[:4])
            month = int(s[4:6])
            q_end = {3: "03-31", 6: "06-30", 9: "09-30", 12: "12-31"}
            day = q_end.get((month - 1) // 3 * 3 + 3)
            return f"{year}-{day}" if day else s
        return s

    @classmethod
    def _clean_financials(cls, rows: list, code: str, end_date: str) -> list["dict"]:
        """从原始行中提取截至 end_date 的干净报告期序列。

        规则：
          - 归一化日期至 ISO，兼容 compact/ISO 混存
          - 丢弃占位/垃圾行（eps、revenue、profit 全为 0）
          - 同一 (code, 报告期) 只保留一条（取最新即日期最大的写入）
          - 保留最新报告期及其前若干季度（供 TTM/FY 口径使用）
          - 仅保留发布于 end_date 之前的报告期，避免前视偏差
        返回按报告期日期升序排列的 dict 列表（保证 financials[-1] 为最新真报告）。
        """
        norm: dict[str, dict] = {}
        for r in rows:
            eps = r["eps"] or 0
            revenue = r["revenue"] or 0
            profit = r["profit"] or 0
            if eps == 0 and revenue == 0 and profit == 0:
                continue
            iso = cls._normalize_financial_date(r["date"])
            if iso > end_date:
                continue
            prev = norm.get(iso)
            if prev is None or r["rowid"] >= prev["_rowid"]:
                d = {"code": code, "date": iso, "_rowid": r["rowid"],
                     "eps": eps, "roe": r["roe"] or 0,
                     "revenue": revenue, "profit": profit,
                     "total_assets": r["total_assets"] or 0,
                     "total_liabilities": r["total_liabilities"] or 0,
                     "net_assets": r["net_assets"] or 0,
                     "operating_cash_flow": r["operating_cash_flow"] or 0,
                     "gross_margin": r["gross_margin"] or 0,
                     "net_margin": r["net_margin"] or 0}
                norm[iso] = d
        return sorted(norm.values(), key=lambda d: d["date"])

    def _get_financials_range(self, conn, code: str, start_date: str, end_date: str) -> list[Financial]:
        try:
            sm = start_date[:7].replace("-", "")
            em = end_date[:7].replace("-", "")
            # D1 PIT 第一段: 过滤键切到 announced_at (真值行=真公告日;
            # 空真值行=期末+45 保守上界 — 不再放行, 前视风险窗口关闭)。
            # 降级: announced_at (D1 回填后) → announce_date / 法定披露截止日 — 每级不炸。
            cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_financials)").fetchall()}
            if "announced_at" in cols:
                rows = conn.execute(
                    "SELECT rowid as rowid, * FROM stock_financials WHERE code = ? AND ("
                    "(date BETWEEN ? AND ?) OR (date BETWEEN ? AND ?)"
                    ") AND (announced_at IS NOT NULL AND announced_at != '' AND announced_at <= ?)"
                    " ORDER BY date",
                    (code, start_date, end_date, sm, em, end_date),
                ).fetchall()
            else:
                # BT-CORRECTNESS ⑦: 公告日为空的行不再放行 (原 DL-47 降级 = 前视)。
                # 可得日 = announce_date (有值) → 法定披露截止日 (Q1 4/30, H1 8/31,
                # Q3 10/31, 年报次年 4/30)。无 announce_date 列 = 旧 schema (DL-47 前),
                # 保持不过滤 (生产表已含该列)。
                from data_layer.pit import financial_available_date
                has_ad = "announce_date" in cols
                raw = conn.execute(
                    "SELECT rowid as rowid, * FROM stock_financials WHERE code = ? AND ("
                    "(date BETWEEN ? AND ?) OR (date BETWEEN ? AND ?)"
                    ") ORDER BY date",
                    (code, start_date, end_date, sm, em),
                ).fetchall()
                rows = [] if has_ad else list(raw)
                for r in (raw if has_ad else []):
                    avail = financial_available_date(r["date"], None, r["announce_date"])
                    if avail is not None and avail <= end_date:
                        rows.append(r)
            cleaned = self._clean_financials(list(rows), code, end_date)
            return [Financial(
                code=d["code"], date=d["date"],
                eps=d["eps"], roe=d["roe"],
                revenue=d["revenue"], profit=d["profit"],
                total_assets=d["total_assets"],
                total_liabilities=d["total_liabilities"],
                net_assets=d["net_assets"],
                operating_cash_flow=d["operating_cash_flow"],
                gross_margin=d["gross_margin"],
                net_margin=d["net_margin"],
            ) for d in cleaned]
        except Exception:
            return []

    def _get_valuations_range(self, conn, code: str, start_date: str, end_date: str) -> list[Valuation]:
        try:
            start_fmt = start_date.replace("-", "")
            end_fmt = end_date.replace("-", "")
            rows = conn.execute(
                "SELECT * FROM stock_valuation WHERE code = ? AND "
                "(date BETWEEN ? AND ? OR date BETWEEN ? AND ?) ORDER BY date",
                (code, start_fmt, end_fmt, start_date, end_date),
            ).fetchall()
            result = []
            for r in rows:
                rk = set(r.keys())
                result.append(Valuation(
                    code=r["code"], date=r["date"],
                    pe_ttm=r["pe_ttm"] if "pe_ttm" in rk else None,
                    pb_mrq=r["pb_mrq"] if "pb_mrq" in rk else None,
                    ps_ttm=r["ps_ttm"] if "ps_ttm" in rk else None,
                    market_cap=r["market_cap"] if "market_cap" in rk else None,
                    total_shares=r["total_shares"] if "total_shares" in rk else None,
                    dividend_yield=r["dividend_yield"] if "dividend_yield" in rk else None,
                ))
            return result
        except Exception:
            return []

    def _get_moneyflow_range(self, conn, code: str, start_date: str, end_date: str) -> list[Moneyflow]:
        try:
            start_fmt = start_date.replace("-", "")
            end_fmt = end_date.replace("-", "")
            rows = conn.execute(
                "SELECT * FROM stock_moneyflow WHERE code = ? AND "
                "(date BETWEEN ? AND ? OR date BETWEEN ? AND ?) ORDER BY date",
                (code, start_fmt, end_fmt, start_date, end_date),
            ).fetchall()
            return [Moneyflow(
                code=r["code"], date=r["date"],
                main_net_inflow=r["main_net_inflow"] or 0,
                retail_net_inflow=r["retail_net_inflow"] or 0,
                main_inflow=r["main_inflow"] or 0,
                main_outflow=r["main_outflow"] or 0,
            ) for r in rows]
        except Exception:
            return []

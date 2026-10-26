"""Fuyao API client — A股财务/行情/热股/异动数据"""

import os
import time
from datetime import datetime, timedelta, timezone

import requests

from common.logging import get_logger

logger = get_logger("data.fuyao_client")

FUYAO_BASE_URL = "https://fuyao.aicubes.cn"
BJT = timezone(timedelta(hours=8))


def _fallback_reports() -> list[str]:
    """Return candidate reports from newest to oldest, to handle not-yet-published quarters."""
    now = datetime.now(BJT)
    q = (now.month - 1) // 3 + 1
    year = now.year
    reports = []
    # QS 卡 financials-q3-window-guard 件① (2026-10-02): 补**当季**入探测列表首位。
    # 旧版从「上季」起 — 当季披露后 (Q3 披露窗 10 月) 仍探不到, 增量闸恒跳过,
    # stock_financials 91 天不进新报告期。当季未披露时探针自然回落旧季
    # (零行为变化); 披露后立即可探。共 5 期。
    for _ in range(5):
        reports.append(f"{year}-{q}")
        if q == 1:
            year -= 1
            q = 4
        else:
            q -= 1
    return reports


# 刀4 (fix/fuyao-circuit-per-family, 2026-10-20): 端点族分桶 —
# 旧实现 _failure_count/_circuit_open_until 是标量类属性, 全端点共享:
# financial 腿 429 风暴打满 → 冷采集腿连坐 (10-08 五表真缺实证)。
# 现按端点族分桶 (financial / shortterm / 未知端点按路径分桶),
# 类级保留 (测试 fixture 改类的卫生习惯 + adapter 每调用新建实例时
# 跨实例仍共享同族桶 — 熔断语义本就该跨该源所有实例)。
_FAMILY_FINANCIAL = "financial"
_FAMILY_SHORTTERM = "shortterm"
_CIRCUIT_FAIL_THRESHOLD = 3
_CIRCUIT_OPEN_SECONDS = 300
_RETRY_429_BACKOFF_S = 2.0

def _endpoint_family(path: str) -> str:
    """端点 → 熔断族 (分桶键)。

    financial: financials/* (季报三表+指标) — fuyao_financial 腿,
        429 风暴源 (逐股×4 端点), 必须与冷采集隔离。
    shortterm: special-data/limit-*/hot-*/skyrocket/anomaly + auction/*
        — shortterm_collect 冷采集腿, 10-08 连坐受害族。
    未知端点: "path:<归一化路径>" — 不落既有桶, 防误连坐 (同文件里
        没注册的端点自己一桶, 坏也只坏自己)。
    """
    p = path or ""
    if "/financials/" in p:
        return _FAMILY_FINANCIAL
    if ("/special-data/" in p or "/auction/" in p):
        return _FAMILY_SHORTTERM
    return "path:" + p


class FuyaoClient:
    # 刀4: 类级分族熔断状态 {family: {"failure_count": int, "open_until": float}}。
    # 类属性 (非模块级) — 保持 FuyaoClient._CIRCUIT 访问习惯 (旧测试
    # FuyaoClient._failure_count 同款), 且 reset_circuit_state() 是唯一
    # 清空入口。族间隔离: financial 开闸不挡 shortterm (10-08 连坐根除)。
    _CIRCUIT: dict[str, dict] = {}

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or os.environ.get("FUYAO_API_KEY", "")
        self._base_url = (base_url or FUYAO_BASE_URL).rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"X-api-key": self.api_key})

    def available(self, family: str | None = None) -> bool:
        """源可用性: 无参=任一桶开闸即不可用 (旧调用点兼容);
        带 family=该族桶状态 (冷采集腿只问自己族, 不被财务腿连坐)。
        """
        if not self.api_key:
            return False
        now = time.time()
        if family is None:
            return all(st["open_until"] <= now
                       for st in self._CIRCUIT.values())
        st = self._CIRCUIT.get(family)
        return st is None or st["open_until"] <= now

    @classmethod
    def _failure_count_for(cls, family: str) -> int:
        return cls._CIRCUIT.get(family, {}).get("failure_count", 0)

    @classmethod
    def _circuit_open_until_for(cls, family: str) -> float:
        return cls._CIRCUIT.get(family, {}).get("open_until", 0.0)

    @classmethod
    def reset_circuit_state(cls) -> None:
        """清空全部族桶 (测试卫生 + 运维手段; 替代旧 fixture 直改类属性)。"""
        cls._CIRCUIT.clear()

    @classmethod
    def _record_failure(cls, family: str, path: str, err: Exception) -> None:
        """记败+开闸 (按族独立)。返回前必须已打日志。"""
        st = cls._CIRCUIT.setdefault(
            family, {"failure_count": 0, "open_until": 0.0})
        st["failure_count"] += 1
        if st["failure_count"] >= _CIRCUIT_FAIL_THRESHOLD:
            st["open_until"] = time.time() + _CIRCUIT_OPEN_SECONDS
            st["failure_count"] = 0
            logger.warning(
                "fuyao 熔断器打开 %ds (族 %s 连续 %d 败) — 该族后续 _get 直接 None",
                _CIRCUIT_OPEN_SECONDS, family, _CIRCUIT_FAIL_THRESHOLD)

    @classmethod
    def _record_success(cls, family: str) -> None:
        st = cls._CIRCUIT.get(family)
        if st is not None:
            st["failure_count"] = 0

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        family = _endpoint_family(path)
        if time.time() < FuyaoClient._CIRCUIT.get(
                family, {}).get("open_until", 0.0):
            return None
        url = f"{self._base_url}{path}"

        def _attempt() -> dict | None:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data")
            # 微卡③ (09-05): 非零 code 是 API 级失败 — 原静默 return None 无痕。
            logger.warning(
                "fuyao _get API code=%s path=%s family=%s failure_count=%d",
                data.get("code"), path, family,
                FuyaoClient._failure_count_for(family))
            return None

        try:
            out = _attempt()
            if out is not None:
                FuyaoClient._record_success(family)
                return out
            return None
        except Exception as e:
            # 微卡③ (09-05): 吞异常零日志 = 09-03 partial 根因链的观测盲区 —
            # 熔断器在循环中段静默开闸 300s, 事后只看见空错误列表。铁律 #12。
            # 刀4: 先记录后打日志 — failure_count 报「含本次」的值,
            # 观测面直接看得见「这是第几败」(微卡③ 契约不丢)。
            FuyaoClient._record_failure(family, path, e)
            logger.warning(
                "fuyao _get 请求异常 path=%s family=%s failure_count=%d err=%s: %.120s",
                path, family, FuyaoClient._failure_count_for(family),
                type(e).__name__, e)
            # 刀4: 单发 429 退避重试一次 — 治 10-09「单发即死刑」
            # (failure_count=1 被相邻成功清零, 熔断永远到不了 3, 但
            # _fetch 单次尝试无重试 = 当日放弃)。限流是间歇态, 等 2s
            # 大概率恢复; 非 429 异常不重试 (快速失败语义保留)。
            if "429" in str(e) or "Too Many Requests" in str(e):
                time.sleep(_RETRY_429_BACKOFF_S)
                try:
                    out = _attempt()
                    if out is not None:
                        FuyaoClient._record_success(family)
                        return out
                except Exception as e2:
                    logger.warning(
                        "fuyao _get 429 重试仍败 path=%s family=%s err=%s: %.120s",
                        path, family, type(e2).__name__, e2)
                    e = e2
            return None

    def get_financial_indicators(self, thscode: str, period: str = "quarterly",
                                  limit: int = 1, report: str | None = None) -> dict | None:
        reports = [report] if report else _fallback_reports()
        for rpt in reports:
            params = {"thscode": thscode, "period": period, "limit": limit, "report": rpt}
            result = self._get("/api/a-share/financials/indicators", params)
            if isinstance(result, dict) and result.get("abilities"):
                return result
        return None

    def get_income_statements(self, thscode: str, period: str = "quarterly",
                               limit: int = 1, report: str | None = None) -> list[dict]:
        reports = [report] if report else _fallback_reports()
        for rpt in reports:
            params = {"thscode": thscode, "period": period, "limit": limit, "report": rpt}
            result = self._get("/api/a-share/financials/income-statements", params)
            data = result.get("item", []) if isinstance(result, dict) else []
            if data:
                return data
        return []

    def get_balance_sheets(self, thscode: str, period: str = "quarterly",
                            limit: int = 1, report: str | None = None) -> list[dict]:
        reports = [report] if report else _fallback_reports()
        for rpt in reports:
            params = {"thscode": thscode, "period": period, "limit": limit, "report": rpt}
            result = self._get("/api/a-share/financials/balance-sheets", params)
            data = result.get("item", []) if isinstance(result, dict) else []
            if data:
                return data
        return []

    def get_cash_flow(self, thscode: str, period: str = "quarterly",
                       limit: int = 1, report: str | None = None) -> list[dict]:
        reports = [report] if report else _fallback_reports()
        for rpt in reports:
            params = {"thscode": thscode, "period": period, "limit": limit, "report": rpt}
            result = self._get("/api/a-share/financials/cash-flow-statements", params)
            data = result.get("item", []) if isinstance(result, dict) else []
            if data:
                return data
        return []

    def get_price_snapshot(self, thscodes: str) -> list[dict]:
        """个股实时行情快照。

        DL-08: 端点返回 {"item": [...], "timestamp", "total"} (dict), 旧实现
        `isinstance(result, list)` 恒 False → 恒返回 [] (snapshot_intraday 实时
        行情长期为空的根因)。修正为取 item。
        """
        result = self._get("/api/a-share/prices/snapshot", {"thscodes": thscodes})
        if isinstance(result, dict):
            return result.get("item", [])
        return result if isinstance(result, list) else []

    def _fetch_paged_pool(self, path: str, date: str | None) -> list[dict]:
        """涨停/跌停池端点返回 {"item": [...], "pagination": {...}}, 需翻页取全量。

        注意: item 内无 "type" 字段; 涨停/跌停是两个独立端点, 由调用方区分。
        """
        items: list[dict] = []
        page = 1
        while page <= 20:  # 安全上限, 防止 pagination 异常导致死循环
            params = {"page": str(page)}
            if date:
                params["date"] = date
            result = self._get(path, params)
            if not isinstance(result, dict):
                break
            batch = result.get("item", [])
            if not batch:
                break
            items.extend(batch)
            pages = (result.get("pagination") or {}).get("pages", 1) or 1
            if page >= pages:
                break
            page += 1
        return items

    def get_limit_up_pool(self, date: str | None = None) -> list[dict]:
        return self._fetch_paged_pool("/api/a-share/special-data/limit-up-pool", date)

    def get_limit_down_pool(self, date: str | None = None) -> list[dict]:
        return self._fetch_paged_pool("/api/a-share/special-data/limit-down-pool", date)

    def get_dragon_tiger_list(self, date: str | None = None) -> list[dict]:
        """龙虎榜 (DL-06) — 返回 stock_items; 单页全量返回, 无分页。

        实测字段 (2026-08-17): thscode/ticker/name/net_value/buy_value/sell_value/
        org_net_value/hot_money_net_value/change/hot_rank/range_days 等。
        """
        params = {}
        if date:
            params["date"] = date
        result = self._get("/api/a-share/special-data/dragon-tiger-list", params)
        if isinstance(result, dict):
            return result.get("stock_items", [])
        return result if isinstance(result, list) else []

    def get_hot_stock_list(self) -> list[dict]:
        result = self._get("/api/a-share/special-data/hot-stock-list")
        return result if isinstance(result, list) else []

    # === DL-07: 不可回填型情绪/短线结构数据端点 (错过当日即永久失去, 冷采集积累) ===

    def get_limit_up_ladder(self, date: str | None = None) -> dict | None:
        """涨停天梯 — 近 30 交易日连板梯队矩阵。

        实测结构 (2026-08-18): {"timestamp", "window": {"length": 30,
        "date_list": [...], "board_caps": {"two_board": 4, ...}},
        "item": [{"date", "boards": {"two_board": [{thscode, ticker, name,
        board_num, seal_nextday, sign_level}, ...], ...}}, ...]}
        """
        params = {}
        if date:
            params["date"] = date
        result = self._get("/api/a-share/special-data/limit-up-ladder", params)
        return result if isinstance(result, dict) else None

    def get_limit_break_pool(self, date: str | None = None) -> list[dict]:
        """炸板池 — 曾涨停但打开的股票池 (分页)。

        实测字段: thscode/ticker/name/last_price/price_change_ratio_pct/
        open_times/turnover_ratio_pct/turnover。
        """
        return self._fetch_paged_pool("/api/a-share/special-data/limit-break-pool", date)

    def get_hot_stock_list_history(self, date: str | None = None) -> list[dict]:
        """历史热股榜 — 按自然日返回热股排行 (可按日回查历史)。

        实测字段: thscode/ticker/name/rank。
        """
        params = {}
        if date:
            params["date"] = date
        result = self._get("/api/a-share/special-data/hot-stock-list-history", params)
        return result.get("item", []) if isinstance(result, dict) else []

    def get_skyrocket_list(self) -> list[dict]:
        """飙升榜 — 热度飙升股票 Top30 (当日快照, 不可回填)。

        实测字段: thscode/ticker/name/rank/heat/rank_change/rank_trend。
        """
        result = self._get("/api/a-share/special-data/skyrocket-list")
        return result.get("item", []) if isinstance(result, dict) else []

    def get_hot_stock_rank_trend(self, thscode: str, start_date: str,
                                  end_date: str) -> list[dict]:
        """热股排名趋势 — 单只股票在日期区间内的热榜排名走势 (逐只查询)。"""
        result = self._get("/api/a-share/special-data/hot-stock-rank-trend",
                           {"thscode": thscode, "start_date": start_date,
                            "end_date": end_date})
        return result.get("item", []) if isinstance(result, dict) else []

    def get_auction_short_term_benchmark(self) -> list[dict]:
        """短线风向标竞价基准 — 集合竞价阶段短线风向标 (盘中快照)。

        实测字段: thscode/ticker/name/auction_pct/tags。
        注意: 正确路径在 /api/a-share/auction/ 下 (非 special-data)。
        """
        result = self._get("/api/a-share/auction/short-term-benchmark")
        return result.get("item", []) if isinstance(result, dict) else []

    def get_auction_snapshot(self, thscodes: str) -> list[dict]:
        """集合竞价快照 — 按 thscodes 批量查询 (9:25 后为 final)。

        实测字段: auction_price/auction_pct/auction_volume/auction_amount/
        auction_unmatched/auction_turnover_pct 等。
        """
        result = self._get("/api/a-share/auction/snapshot", {"thscodes": thscodes})
        return result.get("item", []) if isinstance(result, dict) else []

    def get_calendar(self) -> list[dict]:
        result = self._get("/api/a-share/calendar/trading-days")
        return result if isinstance(result, list) else []

    # === 以下为待上线功能占位 ===
    # def get_valuation(self, thscode: str) -> dict | None:
    #     """获取估值数据(PE/PB/PS 及分位) — 待 Fuyao 上线"""
    #     result = self._get("/api/a-share/valuation/overview", {"thscode": thscode})
    #     return result if isinstance(result, dict) else None

    # def get_stock_info(self, thscode: str) -> dict | None:
    #     """获取股票基础信息 — 待 Fuyao 上线"""
    #     result = self._get("/api/meta/tickers/detail", {"thscode": thscode})
    #     return result if isinstance(result, dict) else None

    def get_anomaly_list(self, tag_codes: list[str] | None = None) -> list[dict]:
        """个股异动原因列表。

        DL-07: 旧端点 /anomaly-list 已 404 (DL-00 实测), 修正为
        /anomaly-analysis-list。实测字段: stock_name/analysis_content/
        keyword_list/thscode/tag_name (无 ticker, 由调用方从 thscode 派生 code)。
        """
        result = self._get("/api/a-share/special-data/anomaly-analysis-list")
        return result.get("item", []) if isinstance(result, dict) else []

    def get_index_snapshot(self, thscodes: str) -> list[dict]:
        """指数实时行情快照。

        DL-08: 与 get_price_snapshot 同病 — 端点返回 {"item": [...]} (dict),
        旧实现恒返回 []。修正为取 item。
        """
        result = self._get("/api/a-share-index/prices/snapshot", {"thscodes": thscodes})
        if isinstance(result, dict):
            return result.get("item", [])
        return result if isinstance(result, list) else []

    def get_index_catalog(self, tag: str = "industry") -> list[dict]:
        """获取板块指数列表: industry=行业板块, cn_concept=概念板块"""
        result = self._get("/api/a-share-index/catalog/ths-index-list", {"tag": tag})
        return result.get("item", []) if isinstance(result, dict) else []


class FuyaoSourceAdapter:
    """Fuyao 数据源适配器"""

    def __init__(self, client=None):
        self._client = client

    @property
    def name(self) -> str:
        return "fuyao"

    @property
    def capabilities(self) -> set[str]:
        # DL-05: capabilities 必须与 get_data 实际分发键一致。
        # 原声明 "index" 但分发键是 index_catalog/index_snapshot → 按 "index" 查必落空 (F-11)。
        # 现改为与分发键一致; 大盘指数 "index" 由 gtimg 提供 (见 gtimg_source.py)。
        # DL-07: 新增不可回填型情绪/短线结构能力键 (冷采集, 不进选股链路)。
        # 待上线: valuation (估值+分位数据), stock_info (基础信息)
        return {"financial", "hot_stock", "anomaly", "limit_up", "limit_down",
                "realtime_quote", "index_catalog", "index_snapshot", "lhb",
                "limit_ladder", "limit_break", "hot_stock_history",
                "skyrocket", "auction_benchmark", "auction_snapshot",
                # DL-31: 三张财务报表 (利润表/资产负债表/现金流量表)
                "income_statement", "balance_sheet", "cash_flow"}

    def is_available(self) -> bool:
        client = self._client or FuyaoClient()
        return client.available()

    @staticmethod
    def _norm_limit_pool(rows: list[dict], date: str, direction: str) -> list[dict]:
        """DL-08: fuyao 涨/跌停池原始字段 → 统一契约 (与 akshare 同构)。

        fuyao 原始: thscode/ticker/name/last_price/price_change_ratio_pct/
        continue_day_cnt; 契约: code/name/date/limit_type/price/change_pct/count。
        不归一化则 limit_up_down 管线 (读 code/price/change_pct/count) 落库全空,
        fusion key_field="code" 对齐也失效 (fuyao 为 limit_up/down 主源, DL-02)。
        """
        records = []
        for r in rows or []:
            code = r.get("ticker") or (r.get("thscode", "") or "").split(".")[0]
            if not code:
                continue
            records.append({
                "code": code,
                "name": r.get("name", ""),
                "date": date,
                "limit_type": direction,
                "price": float(r.get("last_price") or 0),
                "change_pct": float(r.get("price_change_ratio_pct") or 0),
                "count": int(r.get("continue_day_cnt") or 0),
            })
        return records

    @staticmethod
    def _norm_realtime_quote(rows: list[dict]) -> list[dict]:
        """DL-08: fuyao 行情快照原始字段 → 统一契约 (与 gtimg 同构)。

        fuyao 原始: thscode/ticker/last_price/price_change_ratio_pct/open_price/
        high_price/low_price/volume/turnover/prev_price;
        契约: code/name/price/change_pct/open/high/low/volume/amount/pre_close。
        """
        records = []
        for r in rows or []:
            code = r.get("ticker") or (r.get("thscode", "") or "").split(".")[0]
            if not code:
                continue
            records.append({
                "code": code,
                "name": r.get("name", ""),
                "price": float(r.get("last_price") or 0),
                "change_pct": float(r.get("price_change_ratio_pct") or 0),
                "open": float(r.get("open_price") or 0),
                "high": float(r.get("high_price") or 0),
                "low": float(r.get("low_price") or 0),
                "volume": float(r.get("volume") or 0),
                "amount": float(r.get("turnover") or 0),
                "pre_close": float(r.get("prev_price") or 0),
            })
        return records

    def get_data(self, data_type: str, params: dict):
        client = self._client or FuyaoClient()

        if data_type == "financial":
            code = params.get("code", "")
            return client.get_financial_indicators(code, period="quarterly", limit=1)
        elif data_type == "hot_stock":
            return client.get_hot_stock_list()
        elif data_type == "anomaly":
            return client.get_anomaly_list(params.get("tag_codes", []))
        elif data_type == "limit_up":
            date = params.get("date", "")
            return self._norm_limit_pool(client.get_limit_up_pool(date), date, "up")
        elif data_type == "limit_down":
            date = params.get("date", "")
            return self._norm_limit_pool(client.get_limit_down_pool(date), date, "down")
        elif data_type == "realtime_quote":
            codes = params.get("codes", "")
            return self._norm_realtime_quote(client.get_price_snapshot(codes))
        elif data_type == "index_catalog":
            tag = params.get("tag", "industry")
            return client.get_index_catalog(tag)
        elif data_type == "index_snapshot":
            codes = params.get("codes", "")
            return client.get_index_snapshot(codes)
        elif data_type == "lhb":
            # DL-06: fuyao 龙虎榜 → 归一化为 lhb_daily 管线期望的字段
            # (code/buy_in=净买/turnover=总买/capitalization=总卖), 与 zzshare 同构,
            # 供 fusion key_field="code" 对齐交叉验证。
            date = params.get("end_date") or params.get("date", "")
            items = client.get_dragon_tiger_list(date)
            return [
                {
                    "code": it.get("ticker", ""),
                    "buy_in": it.get("net_value"),
                    "turnover": it.get("buy_value"),
                    "capitalization": it.get("sell_value"),
                }
                for it in items if it.get("ticker")
            ]
        # DL-07: 不可回填型情绪/短线结构数据 (冷采集, 原样返回由管线落库)
        elif data_type == "limit_ladder":
            return client.get_limit_up_ladder(params.get("date", ""))
        elif data_type == "limit_break":
            return client.get_limit_break_pool(params.get("date", ""))
        elif data_type == "hot_stock_history":
            return client.get_hot_stock_list_history(params.get("date", ""))
        elif data_type == "skyrocket":
            return client.get_skyrocket_list()
        elif data_type == "auction_benchmark":
            return client.get_auction_short_term_benchmark()
        elif data_type == "auction_snapshot":
            codes = params.get("codes", "")
            return client.get_auction_snapshot(codes)
        # DL-31: 三张财务报表 (季频, 单位=元 CNY, 口径=quarterly)
        elif data_type == "income_statement":
            code = params.get("code", "")
            period = params.get("period", "quarterly")
            limit = int(params.get("limit", 4))
            return client.get_income_statements(code, period=period, limit=limit)
        elif data_type == "balance_sheet":
            code = params.get("code", "")
            period = params.get("period", "quarterly")
            limit = int(params.get("limit", 4))
            return client.get_balance_sheets(code, period=period, limit=limit)
        elif data_type == "cash_flow":
            code = params.get("code", "")
            period = params.get("period", "quarterly")
            limit = int(params.get("limit", 4))
            return client.get_cash_flow(code, period=period, limit=limit)
        # 待上线: valuation, stock_info
        # DL-05: 未知 data_type 显式报错 (原静默返回 None, 调用方无法区分"没数据"和"不支持")
        from common.errors import ErrorSeverity, QuantStarError
        raise QuantStarError(
            layer="data", severity=ErrorSeverity.WARNING,
            trace_id="", recoverable=True,
            suggestion=f"fuyao 不支持 data_type={data_type}")

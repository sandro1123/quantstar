"""AkShare 数据源适配器（含分钟级数据、涨跌停池、财务指标）"""
from datetime import datetime

from data_layer._models import MinuteBar


class AkShareSourceAdapter:
    """AkShare 数据源适配器"""

    @property
    def name(self) -> str:
        return "akshare"

    def __init__(self):
        self.last_failures: list[dict[str, str]] = []
        # DL-28 (D-43): 东财端点降级标记 — {endpoint: {decision, reason, error, ts}}。
        # 软标记: 不屏蔽调用 (封锁是间歇性的), 失败打标, 成功清标 (自动恢复)。
        self.em_downgrades: dict[str, dict] = {}
        # DDP-BUDGET-FIT: 东财端点连续失败熔断器 — {endpoint: consecutive_fail_count}。
        # 连续失败 >= _EM_CIRCUIT_THRESHOLD 时熔断, 后续调用直接返回 None 不发 HTTP。
        # 成功一次即重置 (保留间歇性恢复语义)。每次进程启动重新计数。
        self._em_consec_fail: dict[str, int] = {}

    @property
    def capabilities(self) -> set[str]:
        return {
            "kline", "valuation", "historical_valuation",
            "stock_list", "minute_data",
            "limit_up", "limit_down", "financial",
            "analyst_forecast",
            # DL-07: 板块资金流 (不可回填, 冷采集)
            "sector_fund_flow",
        }

    def is_available(self) -> bool:
        try:
            import akshare as ak  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # DL-28 (D-43): 东财端点统一调用入口 — 软降级标记 + 连续失败熔断
    # ------------------------------------------------------------------

    # DDP-BUDGET-FIT: 连续失败熔断阈值。analyst_forecasts 逐码调 ~5400 次,
    # 若端点被反爬全面封锁, 前 20 次连续失败即熔断, 避免剩余 5380 次空跑。
    # 正常情况 (部分码无研报) 成功与失败交替, 计数器持续重置, 不触发熔断。
    _EM_CIRCUIT_THRESHOLD = 20

    def _em_call(self, ak, endpoint: str, **kwargs):
        """东财系端点统一调用入口 (DL-28, D-43)。

        东财系被应用层反爬拦截 (间歇性), 处置 = 不绕过 + 软降级标记:
        - 失败 → em_downgrades[endpoint] 打标 (已知降级, 记日志而非新告警);
        - 成功 → 清标 (哪天通了自动恢复, 无需改代码, D-43 原文)。
        不在调用前拦截: 封锁是间歇性的 (2026-08-21 sector_fund_flow 仍出数)。
        """
        from data_layer.eastmoney_downgrade import (
            DOWNGRADE_DECISION, DOWNGRADE_REASON, is_eastmoney_endpoint,
        )
        # DDP-BUDGET-FIT: 熔断检查 — 连续失败超阈值则不发 HTTP, 直接返回 None。
        if self._em_consec_fail.get(endpoint, 0) >= self._EM_CIRCUIT_THRESHOLD:
            return None
        try:
            df = getattr(ak, endpoint)(**kwargs)
        except Exception as e:
            if is_eastmoney_endpoint(endpoint):
                self.em_downgrades[endpoint] = {
                    "decision": DOWNGRADE_DECISION,
                    "reason": DOWNGRADE_REASON,
                    "error": str(e)[:200],
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }
                # DDP-BUDGET-FIT: 连续失败计数 + 熔断日志 (仅首次触发时打)
                self._em_consec_fail[endpoint] = self._em_consec_fail.get(endpoint, 0) + 1
                if self._em_consec_fail[endpoint] == self._EM_CIRCUIT_THRESHOLD:
                    __import__("logging").getLogger("data.akshare").warning(
                        f"AkShare {endpoint} 连续失败 {self._EM_CIRCUIT_THRESHOLD} 次, "
                        f"熔断本次运行后续调用 (DDP-BUDGET-FIT)")
                else:
                    __import__("logging").getLogger("data.akshare").warning(
                        f"AkShare {endpoint} failed (已知降级 {DOWNGRADE_DECISION}, "
                        f"东财应用层反爬, 不绕过): {e}")
            raise
        # 成功 → 清标 (自动恢复) + 重置连续失败计数
        self.em_downgrades.pop(endpoint, None)
        self._em_consec_fail.pop(endpoint, None)
        return df

    def get_data(self, data_type: str, params: dict):
        try:
            import akshare as ak
        except ImportError:
            return None

        if data_type == "kline":
            return self._get_klines(ak, params)
        elif data_type == "valuation":
            return self._get_valuation(ak, params)
        elif data_type == "stock_list":
            return self._get_stock_list(ak)
        elif data_type == "minute_data":
            return self._get_minute_data(ak, params)
        elif data_type == "limit_up":
            return self._get_limit_up_pool(ak, params)
        elif data_type == "limit_down":
            return self._get_limit_down_pool(ak, params)
        elif data_type == "financial":
            return self._get_financial(ak, params)
        elif data_type == "analyst_forecast":
            return self._get_analyst_forecast(ak, params)
        elif data_type == "sector_fund_flow":
            return self._get_sector_fund_flow(ak, params)
        return None

    def _get_klines(self, ak, params: dict):
        code = params.get("code", "")
        start = params.get("start_date", "")
        end = params.get("end_date", "")
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust="",  # DL-67: 不复权 — 与库内 raw 口径统一 (DL-19)
        )
        return df.to_dict("records") if not df.empty else []

    @staticmethod
    def _norm_date(value) -> str:
        s = str(value or "")
        compact = s.replace("-", "")
        if len(compact) == 8 and compact.isdigit():
            return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
        return s

    @staticmethod
    def _norm_val_date(value) -> str:
        s = str(value or "")
        compact = s.replace("-", "")
        if len(compact) == 8 and compact.isdigit():
            return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
        return s

    def _get_valuation(self, ak, params: dict):
        code = params.get("code", "")
        date = self._norm_date(params.get("date", ""))
        df = self._em_call(ak, "stock_value_em", symbol=code)
        if df is None or df.empty:
            return None
        df["_d"] = df["数据日期"].astype(str).str.replace("-", "")
        target = date.replace("-", "")
        row = df[df["_d"] == target]
        if row.empty:
            return None
        r = row.iloc[0]
        return self._valuation_record(r, code, date)

    @staticmethod
    def _valuation_record(r, code, date):
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        pe = _f(r.get("PE(TTM)"))
        pb = _f(r.get("市净率"))
        ps = _f(r.get("市销率"))
        mc = _f(r.get("总市值"))
        shares = _f(r.get("总股本"))
        dividend = _f(r.get("股息率"))
        rec = {
            "code": code,
            "date": date,
            "pe_ttm": pe,
            "pb_mrq": pb,
            "ps_ttm": ps,
            "market_cap": mc,
            "total_shares": shares,
            "dividend_yield": dividend,
        }
        if not any(value is not None and value != 0.0
                   for value in [pe, pb, ps, mc]):
            return None
        return rec

    def get_batch(
        self, data_type: str, params: dict,
        max_individual_fallback: int | None = None,
    ) -> list[dict]:
        import concurrent.futures as _futures
        if data_type not in {"valuation", "historical_valuation"}:
            return []
        import akshare as ak
        symbols = list(
            dict.fromkeys(str(code) for code in params.get("symbols") or [])
        )
        if not symbols:
            return []
        date = self._norm_date(params.get("date") or "")
        records: list[dict] = []
        # 单股拉取东财估值：并行拉取 + 短间隔限速防止反爬。
        # 串行 ~0.3s/只 × 5000 = 25min+；并行 8 路可将全市场压缩到 ~3min。
        workers = min(8, max(1, len(symbols)))

        def _fetch_one(code: str):
            try:
                rec = self._get_valuation(ak, {"code": code, "date": date})
                if rec:
                    return rec
            except Exception:
                pass
            return None

        with _futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for rec in pool.map(_fetch_one, symbols):
                if rec:
                    records.append(rec)
        self.last_batch_failures = self.last_failures
        return records

    def _get_stock_list(self, ak):
        df = self._em_call(ak, "stock_zh_a_spot_em")
        return df[["代码", "名称"]].to_dict("records") if not df.empty else []

    def _get_minute_data(self, ak, params: dict) -> list[MinuteBar]:
        """获取分钟K线（1/5/15/30/60分钟）"""
        code = params.get("code", "")
        period = params.get("period", "1")
        count = params.get("count", 240)
        date = params.get("date") or datetime.now().strftime("%Y%m%d")
        try:
            df = self._em_call(
                ak, "stock_zh_a_hist_min_em",
                symbol=code, period=period,
                start_date=date, end_date=date, adjust="",
            )
            if df is None or df.empty:
                return []
            records = df.to_dict("records")
            bars = []
            for r in records:
                t = str(r.get("时间", ""))
                bars.append(MinuteBar(
                    code=code,
                    time=t,
                    open=float(r.get("开盘", 0) or 0),
                    high=float(r.get("最高", 0) or 0),
                    low=float(r.get("最低", 0) or 0),
                    close=float(r.get("收盘", 0) or 0),
                    volume=float(r.get("成交量", 0) or 0),
                ))
            return bars[:count]
        except Exception as e:
            __import__("logging").getLogger("data.akshare").warning(f"AkShare minute data failed: {e}")
            return []

    def get_minute_kline(self, code: str, period: str = "1", count: int = 240) -> list[MinuteBar]:
        """公开方法：获取分钟K线"""
        import akshare as ak
        return self._get_minute_data(ak, {"code": code, "period": period, "count": count})

    # ------------------------------------------------------------------
    # 涨跌停池（东方财富 via akshare）
    # ------------------------------------------------------------------

    @staticmethod
    def _to_compact_date(value: str) -> str:
        """'2026-08-13' -> '20260813' (akshare 涨停接口需紧凑日期)"""
        return str(value or "").replace("-", "")

    def _get_limit_up_pool(self, ak, params: dict) -> list[dict]:
        """涨停股池 — ak.stock_zt_pool_em(date)

        返回统一格式: [{code, name, date, limit_type, price, change_pct, count, ...}]
        字段映射: 代码→code, 名称→name, 涨跌幅→change_pct, 最新价→price,
                  连板数→count
        """
        date = self._to_compact_date(params.get("date", ""))
        if not date:
            return []
        try:
            df = self._em_call(ak, "stock_zt_pool_em", date=date)
            if df is None or df.empty:
                return []
        except Exception as e:
            __import__("logging").getLogger("data.akshare").warning(
                f"AkShare limit_up_pool failed: {e}")
            return []
        records = []
        for _, r in df.iterrows():
            records.append({
                "code": str(r.get("代码", "")),
                "name": str(r.get("名称", "")),
                "date": params.get("date", ""),
                "limit_type": "up",
                "price": float(r.get("最新价", 0) or 0),
                "change_pct": float(r.get("涨跌幅", 0) or 0),
                "count": int(r.get("连板数", 0) or 0),
            })
        return records

    def _get_limit_down_pool(self, ak, params: dict) -> list[dict]:
        """跌停股池 — ak.stock_zt_pool_dtgc_em(date)

        返回统一格式: [{code, name, date, limit_type, price, change_pct, count, ...}]
        字段映射: 代码→code, 名称→name, 涨跌幅→change_pct, 最新价→price,
                  连续跌停→count
        """
        date = self._to_compact_date(params.get("date", ""))
        if not date:
            return []
        try:
            df = self._em_call(ak, "stock_zt_pool_dtgc_em", date=date)
            if df is None or df.empty:
                return []
        except Exception as e:
            __import__("logging").getLogger("data.akshare").warning(
                f"AkShare limit_down_pool failed: {e}")
            return []
        records = []
        for _, r in df.iterrows():
            records.append({
                "code": str(r.get("代码", "")),
                "name": str(r.get("名称", "")),
                "date": params.get("date", ""),
                "limit_type": "down",
                "price": float(r.get("最新价", 0) or 0),
                "change_pct": float(r.get("涨跌幅", 0) or 0),
                "count": int(r.get("连续跌停", 0) or 0),
            })
        return records

    # ------------------------------------------------------------------
    # 板块资金流 (同花顺 10jqka via akshare, DL-07 冷采集; #116 面二 swap)
    # ------------------------------------------------------------------

    def _get_sector_fund_flow(self, ak, params: dict) -> list[dict]:
        """板块资金流 — ak.stock_fund_flow_industry/concept(symbol="即时")

        不可回填的当日快照 (DL/07)。拉行业+概念两类, 归一化为
        [{sector_name, sector_type, change_pct, main_net_amount, ...}]。

        #116 面二 swap (2026-09-05): 东财端点 stock_sector_fund_flow_rank
        08-22 起被应用层反爬 RemoteDisconnected (D/43 已知降级, 不绕过),
        且只供当日快照 → 10 个交易日缺口结构性不可回补。修法 = 换通道不换
        架构: 切到同花顺 10jqka 通道。东财若恢复亦不回切 — 单一供数通道,
        架构层选型决策 (记档 #116 卷宗)。

        字段映射 (10jqka 实测列名, 探针 2026-09-05 01:37):
          直映 5: 行业→sector_name / 行业-涨跌幅→change_pct / 净额→
            main_net_amount (净额=流入资金-流出资金, 同源交叉口径) /
            领涨股→top_stock / sector_type label 照旧。
          置 NULL 5: main_net_ratio + 大中小单四拆分 — 10jqka 不供占比与
            大小单拆分, 宁 NULL 不假值 (消费面已审计: 全仓零算术消费者,
            writer _safe_float(None)→NULL 构造性安全; #122 判例)。
          不映射 4: 公司家数/行业指数/领涨股-涨跌幅/当前价 — 10jqka 独有,
            不扩表, 零架构变更。
        """
        import time
        records: list[dict] = []
        for endpoint, label in (
                ("stock_fund_flow_industry", "industry"),
                ("stock_fund_flow_concept", "concept")):
            df = None
            # 重试 1 次结构保留 (拉类接口间歇性远端; 10jqka 白日时段敏感
            # 风险同族 — zzshare 判例, 首验含 15:55 白日窗真值判读)。
            # 数据不可回填, 尽量抓住可用窗口。
            for attempt in range(2):
                try:
                    df = getattr(ak, endpoint)(symbol="即时")
                    break
                except Exception as e:
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    __import__("logging").getLogger("data.akshare").warning(
                        f"AkShare sector_fund_flow({label}) failed: {e}")
            if df is None or df.empty:
                continue

            def _f(r, key: str):
                try:
                    v = r.get(key)
                    if v is None:
                        return None
                    fv = float(v)
                    import math
                    return None if math.isnan(fv) else fv
                except (TypeError, ValueError):
                    return None

            for _, r in df.iterrows():
                records.append({
                    "sector_name": str(r.get("行业", "") or ""),
                    "sector_type": label,
                    "change_pct": _f(r, "行业-涨跌幅"),
                    "main_net_amount": _f(r, "净额"),
                    # 10jqka 不供占比/大小单拆分 → 宁 NULL 不假值 (#116/#122)
                    "main_net_ratio": None,
                    "super_large_amount": None,
                    "large_amount": None,
                    "medium_amount": None,
                    "small_amount": None,
                    "top_stock": str(r.get("领涨股", "") or ""),
                })
        return records

    # ------------------------------------------------------------------
    # 财务指标（东方财富 via akshare）
    # ------------------------------------------------------------------

    def _get_financial(self, ak, params: dict) -> dict | None:
        """财务指标 — ak.stock_financial_analysis_indicator(symbol, start_year)

        返回最新一期的财务指标, 字段映射到 FinancialRepo.batch_upsert 所需格式:
        eps, roe, revenue, profit, total_assets, total_liabilities, net_assets,
        gross_margin, net_margin

        注意: akshare 返回的单位是"元"不是"万元", 列名与文档可能有差异,
        此处使用实际探测到的列名。NaN 值统一转 0.0。
        """
        import math
        code = params.get("code", "")
        if not code:
            return None
        start_year = str(params.get("start_year", ""))
        if not start_year:
            start_year = str(datetime.now().year - 1)
        try:
            df = ak.stock_financial_analysis_indicator(
                symbol=code, start_year=start_year)
            if df is None or df.empty:
                return None
        except Exception as e:
            __import__("logging").getLogger("data.akshare").warning(
                f"AkShare financial({code}) failed: {e}")
            return None

        row = df.iloc[0]
        date_str = str(row.get("日期", ""))

        def _f(key: str) -> float:
            """安全取 float, NaN → 0.0"""
            try:
                v = row.get(key)
                if v is None:
                    return 0.0
                fv = float(v)
                return 0.0 if math.isnan(fv) else fv
            except (TypeError, ValueError):
                return 0.0

        # 总资产(元) -> 万元
        total_assets = _f("总资产(元)") / 1e4
        # 资产负债率(%) 推算总负债
        debt_ratio = _f("资产负债率(%)")
        total_liabilities = total_assets * debt_ratio / 100.0 if total_assets and debt_ratio else 0.0
        # 净资产 = 总资产 - 总负债
        net_assets = total_assets - total_liabilities if total_assets else 0.0
        # 主营业务利润(元) -> 万元 作为 revenue 近似
        revenue = _f("主营业务利润(元)") / 1e4
        # 扣非净利润(元) -> 万元 作为 profit 近似
        profit = _f("扣除非经常性损益后的净利润(元)") / 1e4

        return {
            "code": code,
            "date": date_str[:10] if date_str else params.get("date", ""),
            "eps": _f("摊薄每股收益(元)"),
            "roe": _f("净资产收益率(%)"),
            "revenue": revenue,
            "profit": profit,
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "net_assets": net_assets,
            "gross_margin": _f("销售毛利率(%)"),
            "net_margin": _f("销售净利率(%)"),
        }

    def _get_analyst_forecast(self, ak, params: dict):
        code = params.get("code", "")
        if not code:
            return None
        try:
            df = self._em_call(ak, "stock_research_report_em", symbol=code)
        except Exception:
            return None
        if df is None or len(df) == 0:
            return None
        from datetime import datetime
        current_year = datetime.now().year
        latest = df.iloc[0]
        eps_values = []
        for y in range(current_year, current_year + 3):
            col = f"{y}-盈利预测-收益"
            val = float(latest.get(col, 0) or 0)
            if val > 0:
                eps_values.append(val)
        eps_next = eps_values[0] if len(eps_values) > 0 else 0.0
        eps_year_after = eps_values[1] if len(eps_values) > 1 else 0.0
        rating = str(latest.get("东财评级", "") or "")
        target_price = float(latest.get(f"{current_year}-盈利预测-市盈率", 0) or 0) * eps_next if eps_next > 0 else 0
        consensus = (eps_next + eps_year_after) / 2 if eps_next > 0 or eps_year_after > 0 else 0.0
        return {
            "code": code,
            "consensus_score": consensus,
            "target_price": target_price,
            "rating": rating,
        }

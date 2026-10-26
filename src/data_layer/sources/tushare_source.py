"""Tushare 数据源适配器 — K线/估值/股票列表"""
import os
import random
import time
import unicodedata
from typing import Any, Callable

from common.logging import get_logger

from data_layer._models import Kline

logger = get_logger("data_layer.sources.tushare")


class TushareSourceAdapter:
    name = "tushare"
    capabilities = {"kline", "kline_bulk", "valuation", "historical_valuation", "stock_list", "float_share"}

    # ddp 两小卡①: bulk 路径重试参数。上限 3 次 = 1 次原始调用 + 2 次重试,
    # 网络抖动退避 2s/4s；核心 daily 的明确分钟限频最多等两个 65~70s 窗口。
    # 日配额/权限及未知窗口限频快速失败，避免重复消耗已耗尽的配额。
    BULK_MAX_ATTEMPTS = 3
    BULK_BACKOFF_BASE_S = 2.0
    BULK_RATE_LIMIT_BACKOFF_S = 65.0
    BULK_RATE_LIMIT_JITTER_S = 5.0

    def __init__(self):
        self._pro = None
        # DL-61②: daily_basic 按 trade_date 进程内共享缓存 (存整张 DataFrame)。
        # kline bulk (get_daily_market 取 turnover_rate) 与 valuation
        # (get_daily_valuation 取 pe/pb/ps/mv/turnover_rate) 都需要 daily_basic;
        # 缓存保证同一交易日只调一次, 不在已限频出过事的接口上加调用
        # (08-11 tushare 20000次/天 超限事故)。
        self._daily_basic_cache: dict[str, Any] = {}

    def _get_pro(self):
        if self._pro is None:
            import tushare as ts
            token = os.environ.get("TUSHARE_TOKEN", "")
            if token:
                ts.set_token(token)
                self._pro = ts.pro_api()
        return self._pro

    @staticmethod
    def _normalized_error(exc: Exception) -> str:
        """上游异常没有专用类型；全半角/空白差异不得断开退避链。"""
        text = unicodedata.normalize("NFKC", str(exc)).casefold()
        return "".join(c for c in text if not c.isspace()
                       and not unicodedata.category(c).startswith("P"))

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """网络/分钟限频可重试；日配额、权限优先拒绝，未知错误快速失败。"""
        msg = TushareSourceAdapter._normalized_error(exc)
        # 日配额/权限错误即使命中限频词也不能重试 (08-11 日配额事故)。
        if any(k in msg for k in (
                "每天", "每日", "次天", "次日", "dailyratelimit", "perday",
                "dayquota", "quota", "配额", "permission", "权限", "积分不足",
                "tokeninvalid", "badparam")):
            return False
        if TushareSourceAdapter._is_minute_limit(exc):
            return True
        if any(k in msg for k in ("toomany", "限频", "频率", "最多访问")):
            return False
        return any(k in msg for k in (
            "timeout", "timedout", "connection", "network", "temporarily",
            "服务器", "servererror", "500", "502", "503", "504"))

    @staticmethod
    def _is_minute_limit(exc: Exception) -> bool:
        # 09-16 实际报错为 频率超限(50次/分钟)，旧 每分钟 词表漏判。
        # rate limit/频率超限 使用一分钟保守等待；是否可重试先由上方
        # 配额/权限否决。裸 too many 不推断窗口。
        msg = TushareSourceAdapter._normalized_error(exc)
        return any(k in msg for k in (
            "频率超限", "次分钟", "每分钟", "ratelimit"))

    # DDP 僵死根因修 (09-10 病理): 单次尝试墙钟护栏。
    _WALLCLOCK_TIMEOUT_S = 60.0

    def _retry_call(self, desc: str, fn: Callable[[], Any]) -> Any:
        """bulk 路径统一重试+指数退避+墙钟护栏 (ddp 两小卡①+僵死修)。

        墙钟护栏 (DL-50 get_float_shares 先例同款): 每次尝试放 daemon 线程 +
        join(timeout=60s)。tushare SDK 内部 requests 无 timeout — 网络层
        内核级挂起 (09-10 15:30 ddp 50 分钟静默→zombie reap→晚报全链断的
        病根) 时线程永不返回, join 强断后按本次尝试失败处理走重试/上抛。
        单 helper 预算: 网络最多 186s；限频最多 3×60+2×70=320s。
        核心 daily 最多两次完整分钟窗口等待；daily_basic 限频直接降级。
        此非整腿上界：之后可能还有 daily_basic/备源。kline 默认子腿
        1800s、顶层补跑 600s；本 helper 不增改这些调用方配置。

        与旧实现的差别: 旧 pro.daily()/daily_basic() 裸调, 网络抖动一次即
        空表/异常 — 回退日 bulk 路径静默吞错 (B 阵亡前 08-28 ddp 派单遗留卡)。
        现在: 可重试异常最多 BULK_MAX_ATTEMPTS 次, 每次失败记 warning 日志
        (含次数与退避秒数, 铁律 #12 可观测); 重试穷尽仍失败 → 上抛, 由
        kline 管道 except 收进 errors 落 pipeline_runs (诚实残留, 宁 NULL
        不假值)。返回值不做任何修饰 — 空表就是空表, 不拿重试后的空表冒充成功。
        """
        import threading

        last_exc: Exception | None = None
        for attempt in range(1, self.BULK_MAX_ATTEMPTS + 1):
            holder: list = []

            def _run():
                try:
                    holder.append(fn())
                except Exception as e:  # noqa: BLE001
                    holder.append(e)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=self._WALLCLOCK_TIMEOUT_S)
            if t.is_alive():
                # 挂起 (线程永不返回) — 墙钟强断, 本次尝试记超时失败
                wall_exc = TimeoutError(
                    f"tushare {desc} 墙钟超时 ({self._WALLCLOCK_TIMEOUT_S:.0f}s) "
                    f"— 网络层挂起, attempt={attempt}/{self.BULK_MAX_ATTEMPTS}")
                logger.warning("tushare bulk wallclock: %s", str(wall_exc)[:140])
                if attempt < self.BULK_MAX_ATTEMPTS:
                    backoff = self.BULK_BACKOFF_BASE_S * (2 ** (attempt - 1))
                    time.sleep(backoff)
                last_exc = wall_exc
                continue
            if holder and isinstance(holder[0], Exception):
                exc = holder[0]
            elif holder:
                return holder[0]
            else:  # 理论不可达 (线程结束但无结果) — 按超时处理
                exc = TimeoutError(f"tushare {desc} 无结果返回")
                last_exc = exc
                continue
            last_exc = exc
            if not self._is_retryable(exc):
                raise exc
            minute_limit = self._is_minute_limit(exc)
            if minute_limit and desc != "daily":
                raise exc
            if attempt < self.BULK_MAX_ATTEMPTS:
                if minute_limit:
                    backoff = (self.BULK_RATE_LIMIT_BACKOFF_S + random.uniform(
                        0.0, self.BULK_RATE_LIMIT_JITTER_S))
                else:
                    backoff = self.BULK_BACKOFF_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "tushare bulk retry %s attempt=%d/%d backoff=%.0fs "
                    "err=%s", desc, attempt, self.BULK_MAX_ATTEMPTS,
                    backoff, str(exc)[:120])
                time.sleep(backoff)
            elif minute_limit:
                logger.warning(
                    "tushare bulk rate limit exhausted %s attempt=%d/%d err=%s",
                    desc, attempt, self.BULK_MAX_ATTEMPTS, str(exc)[:120])
        raise last_exc  # 穷尽重试 — 上抛交降级链/pipeline_runs 诚实残留

    def _get_daily_basic(self, trade_date: str):
        """DL-61②: 返回某 trade_date 的 daily_basic DataFrame, 进程内缓存。

        失败/空 → 缓存并返回 None (调用方各自降级, 不阻断主数据写入)。
        ddp 两小卡①: daily_basic 调用改走 _retry_call — 失败可重试而非一次
        吞掉; 穷尽后仍 None 缓存 (降级语义不变, 但失败痕迹有日志)。
        """
        if trade_date in self._daily_basic_cache:
            return self._daily_basic_cache[trade_date]
        dfb = None
        pro = self._get_pro()
        if pro is not None:
            try:
                cand = self._retry_call(
                    "daily_basic",
                    lambda: pro.daily_basic(trade_date=trade_date))
                if cand is not None and not cand.empty:
                    dfb = cand
            except Exception as exc:
                logger.warning(
                    "tushare daily_basic %s 获取失败 (重试穷尽或不可重试): %s",
                    trade_date, str(exc)[:120])
                dfb = None
        self._daily_basic_cache[trade_date] = dfb
        return dfb

    def is_available(self) -> bool:
        try:
            import tushare as ts  # noqa: F401
            return bool(os.environ.get("TUSHARE_TOKEN", ""))
        except ImportError:
            return False

    def get_data(self, data_type: str, params: dict) -> Any:
        pro = self._get_pro()
        if pro is None:
            return None
        if data_type == "kline":
            return self._get_klines(pro, params)
        elif data_type in ("valuation", "historical_valuation"):
            return self._get_valuation(pro, params)
        elif data_type == "stock_list":
            return self._get_stock_list(pro)
        elif data_type == "delist":
            return self._get_delist_list(pro)
        elif data_type == "float_share":
            return self.get_float_shares(params.get("date", ""))
        return None

    def get_daily_market(self, date: str) -> list[dict]:
        """Fetch one full-market daily snapshot in a single API call.

        ddp 两小卡①: pro.daily() 改走 _retry_call (重试+退避)。穷尽重试后
        异常上抛 — 由 kline 管道 except 收进 errors 落 pipeline_runs,
        不再静默; 空表 (df 为 None/empty) 仍返回 [] 由 _BULK_MIN_ROWS
        阈值拒收 — 空表不冒充成功, 原语义保留。
        """
        pro = self._get_pro()
        if pro is None:
            return []
        trade_date = date.replace("-", "")
        df = self._retry_call("daily", lambda: pro.daily(trade_date=trade_date))
        if df is None or df.empty:
            return []
        # DL-61②: pro.daily() 不含 turnover_rate (在 daily_basic 里)。
        # 此前硬编码 0.0 → daily_klines 全表 turnover_rate 恒 0,
        # 打死 liquidity_illiquid/avg_turnover_rate/turnover_ratio 等因子 (DL-66 普查)。
        # 经 _get_daily_basic 共享 daily_basic 结果 (与 valuation 同一次调用);
        # 失败保持 0.0 不阻断 K 线写入。
        dfb = self._get_daily_basic(trade_date)
        tr_map: dict[str, float] = {}
        if dfb is not None:
            for _, brow in dfb.iterrows():
                tr_map[str(brow.get("ts_code", "")).split(".", 1)[0]] = float(
                    brow.get("turnover_rate", 0) or 0)
        records = []
        for _, row in df.iterrows():
            raw_date = str(row.get("trade_date", trade_date))
            normalized_date = (
                f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                if len(raw_date) == 8 else date
            )
            code = str(row.get("ts_code", "")).split(".", 1)[0]
            records.append({
                "code": code,
                "date": normalized_date,
                "open": float(row.get("open", 0) or 0),
                "high": float(row.get("high", 0) or 0),
                "low": float(row.get("low", 0) or 0),
                "close": float(row.get("close", 0) or 0),
                "volume": float(row.get("vol", 0) or 0) * 100,
                "amount": float(row.get("amount", 0) or 0) * 1000,
                "pre_close": float(row.get("pre_close", 0) or 0),
                "change_pct": float(row.get("pct_chg", 0) or 0),
                "turnover_rate": tr_map.get(code, 0.0),
            })
        return records

    def get_daily_valuation(self, date: str) -> list[dict]:
        """Fetch one full-market valuation snapshot in a single API call."""
        trade_date = date.replace("-", "")
        # DL-61②: 走共享缓存, 与 kline bulk 的 turnover_rate 合并共用同一次调用。
        df = self._get_daily_basic(trade_date)
        if df is None or df.empty:
            return []
        records = []
        for _, row in df.iterrows():
            records.append({
                "code": str(row.get("ts_code", "")).split(".", 1)[0],
                "date": date,
                "pe_ttm": float(row.get("pe_ttm", 0) or 0),
                "pb_mrq": float(row.get("pb", 0) or 0),
                "ps_ttm": float(row.get("ps_ttm", 0) or 0),
                "market_cap": float(row.get("total_mv", 0) or 0) * 10000,
                "total_shares": float(row.get("total_share", 0) or 0) * 10000,
                "dividend_yield": float(row.get("dv_ttm", 0) or 0),
                "turnover_rate": float(row.get("turnover_rate", 0) or 0),
                "float_share": float(row.get("float_share", 0) or 0) * 10000,  # DL-50: 万股→股
            })
        return records

    def _get_klines(self, pro, params: dict) -> list[Kline]:
        code = params.get("code", "")
        start = params.get("start_date", "")
        end = params.get("end_date", "")
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        # DL-67: 不传 adj (tushare 默认不复权) — 与 bulk 主路径 get_daily_market
        # 及库内 raw 口径统一 (DL-19 定论: 库内 close 为未复权价)。
        df = pro.daily(ts_code=ts_code, start_date=start.replace("-", ""), end_date=end.replace("-", ""))
        if df is None or df.empty:
            return []
        result = []
        for _, r in df.iterrows():
            close = float(r.get("close", 0) or 0)
            pre_close = float(r.get("pre_close", 0) or 0)
            change_pct = round((close - pre_close) / pre_close * 100, 2) if pre_close else 0.0
            td = str(r.get("trade_date", ""))
            date = f"{td[:4]}-{td[4:6]}-{td[6:8]}" if len(td) == 8 else td
            result.append(Kline(
                code=code,
                date=date,
                open=float(r.get("open", 0) or 0),
                high=float(r.get("high", 0) or 0),
                low=float(r.get("low", 0) or 0),
                close=close,
                volume=float(r.get("vol", 0) or 0) * 100,  # tushare vol unit = shou, x100 -> gu (parity with bulk get_daily_market)
                amount=float(r.get("amount", 0) or 0) * 1000,  # tushare amount unit = qian-yuan, x1000 -> yuan (parity with bulk)
                pre_close=pre_close,
                change_pct=change_pct,
                turnover_rate=float(r.get("turnover_rate", 0) or 0) if "turnover_rate" in r else 0.0,
            ))
        return result

    def _get_valuation(self, pro, params: dict) -> dict | None:
        code = params.get("code", "")
        date = params.get("date", "")
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        df = pro.daily_basic(ts_code=ts_code, start_date=date.replace("-", ""), end_date=date.replace("-", ""))
        if df is None or df.empty:
            return None
        row = df.iloc[0]
        return {
            "code": code,
            "date": date,
            "pe_ttm": float(row.get("pe_ttm", 0) or 0),
            "pb_mrq": float(row.get("pb_mrq", 0) or 0),
            "ps_ttm": float(row.get("ps_ttm", 0) or 0) if "ps_ttm" in row else 0.0,
            "market_cap": float(row.get("total_mv", 0) or 0) if "total_mv" in row else 0.0,
        }

    def _get_stock_list(self, pro) -> list[dict]:
        df = pro.stock_basic(exchange="", list_status="L")
        if df is None or df.empty:
            return []
        # DL-21: stock_basic 原生含 list_date, 不再丢弃
        return [{"code": r["symbol"], "name": r["name"],
                 "industry": r.get("industry", ""),
                 "list_date": str(r.get("list_date", "") or "")}
                for _, r in df.iterrows()]

    def _get_delist_list(self, pro) -> list[dict]:
        """退市股票清单 — stock_basic(list_status=D) 含 delist_date。

        DL-XX: 供 stock_list 管道精确化 delist_date (机会性, 失败不致命)。
        """
        df = pro.stock_basic(exchange="", list_status="D")
        if df is None or df.empty:
            return []
        return [{"code": r["symbol"], "name": r["name"],
                 "delist_date": str(r.get("delist_date", "") or "")}
                for _, r in df.iterrows()]

    # ---- 流通股本 (DL-50) ----
    def get_float_shares(self, date: str) -> dict[str, float]:
        """按日全市场流通股本 — 复用 daily_basic 的 float_share 字段。

        DL-50: 源接入卡, 不写库。返回 {code: float_shares(股)} 供未来因子消费。
        单位: tushare float_share 单位=万股, 此处转为股 (×10000)。
        墙钟护栏: tushare HTTP 调用自带 requests timeout (默认 30s),
        额外加 threading.Timer 硬超时兜底 (防 DNS 挂死等极端场景)。
        """
        import threading

        pro = self._get_pro()
        if pro is None:
            return {}
        trade_date = date.replace("-", "")

        result_holder: list = []
        error_holder: list = []

        def _fetch():
            try:
                df = pro.daily_basic(trade_date=trade_date)
                result_holder.append(df)
            except Exception as e:  # noqa: BLE001
                error_holder.append(e)

        # DL-50 墙钟护栏: daemon 线程 + join(timeout), 不用 concurrent.futures
        # (其非 daemon 线程在 atexit 被 join → P0-7 ghost)
        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        t.join(timeout=60.0)
        if t.is_alive():
            import logging
            logging.getLogger("data.tushare").error(
                f"tushare float_share {date} 墙钟超时 (60s)")
            return {}
        if error_holder:
            import logging
            logging.getLogger("data.tushare").warning(
                f"tushare float_share {date} failed: {error_holder[0]}")
            return {}
        if not result_holder:
            return {}

        df = result_holder[0]
        if df is None or df.empty:
            return {}
        out: dict[str, float] = {}
        for _, row in df.iterrows():
            code = str(row.get("ts_code", "")).split(".", 1)[0]
            fs = float(row.get("float_share", 0) or 0)
            if code and fs > 0:
                out[code] = fs * 10000  # 万股 → 股
        return out

"""AkShare 新浪源适配器 — K线数据（新浪财经，非东财）"""
from typing import Any

from data_layer._models import Kline


class AkshareSinaAdapter:
    name = "akshare_sina"
    capabilities = {"kline", "stock_list"}

    def is_available(self) -> bool:
        try:
            import akshare as ak  # noqa: F401
            return True
        except ImportError:
            return False

    def get_data(self, data_type: str, params: dict) -> Any:
        try:
            import akshare as ak
        except ImportError:
            return None
        if data_type == "kline":
            return self._get_klines(ak, params)
        elif data_type == "stock_list":
            return self._get_stock_list(ak)
        return None

    def _ths_code(self, code: str) -> str:
        if code.startswith(("92", "43", "83", "87", "4", "8")):
            return f"bj{code}"
        return f"sh{code}" if code.startswith("6") else f"sz{code}"

    def _get_klines(self, ak, params: dict) -> list[Kline]:
        code = params.get("code", "")
        start = params.get("start_date", "")
        end = params.get("end_date", "")
        ths = self._ths_code(code)
        # DL-67: adjust="" 不复权 — 与库内 raw 口径统一 (DL-19)
        df = ak.stock_zh_a_daily(symbol=ths, start_date=start.replace("-", ""),
                                 end_date=end.replace("-", ""), adjust="")
        if df is None or df.empty:
            return []
        result = []
        for _, r in df.iterrows():
            kdate = str(r.get("date", ""))[:10]
            if kdate < start or kdate > end:
                continue
            open_p = float(r.get("open", 0) or 0)
            close = float(r.get("close", 0) or 0)
            pre_close = float(r.get("pre_close", 0) or 0)
            # F-23 (2026-10-22): 停牌日 close=0 直接落库 = 0 价行 (79 行/14 只).
            # 停牌不是跌停 —— 与 baostock_source.py:416 的
            # `if tradestatus != '1': continue` 对齐: close<=0 直接跳过该行,
            # 不产出 Kline (避免 0 价污染落库与下游因子/回测守卫).
            if close <= 0:
                continue
            if pre_close and pre_close > 0:
                change_pct = round((close - pre_close) / pre_close * 100, 2)
            elif open_p and open_p > 0:
                change_pct = round((close - open_p) / open_p * 100, 2)
            else:
                change_pct = 0.0
            result.append(Kline(
                code=code, date=kdate,
                open=open_p, high=float(r.get("high", 0) or 0),
                low=float(r.get("low", 0) or 0), close=close,
                volume=float(r.get("volume", 0) or 0),
                amount=float(r.get("amount", 0) or 0),
                change_pct=change_pct,
            ))
        return result

    def _get_stock_list(self, ak) -> list[dict]:
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            return []
        return [{"code": r["code"], "name": r["name"]} for _, r in df.iterrows()]

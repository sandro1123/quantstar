"""同花顺 10jqka 数据源适配器 — 个股资金流"""
from typing import Any


def _parse_amount(s: str) -> float:
    """解析金额字符串: '-41.58万' → -415800, '756.77万' → 7567700, '1.23亿' → 123000000"""
    if not s or not isinstance(s, str):
        return 0.0
    s = s.strip().replace(",", "")
    if "亿" in s:
        return float(s.replace("亿", "")) * 1e8
    elif "万" in s:
        return float(s.replace("万", "")) * 1e4
    try:
        return float(s)
    except ValueError:
        return 0.0


class TonghuashunSourceAdapter:
    name = "tonghuashun"
    capabilities = {"moneyflow"}

    def __init__(self):
        self._bulk_cache: dict[str, dict] | None = None

    def is_available(self) -> bool:
        try:
            import akshare as ak  # noqa: F401
            return True
        except ImportError:
            return False

    def get_data(self, data_type: str, params: dict) -> Any:
        if data_type == "moneyflow":
            return self._get_moneyflow(params)
        return None

    def get_all_moneyflow(self) -> dict[str, dict]:
        """批量获取全市场资金流（单次API调用），返回 {code: data}"""
        if self._bulk_cache is not None:
            return self._bulk_cache
        try:
            import akshare as ak
            df = ak.stock_fund_flow_individual(symbol="即时")
            if df is None or df.empty:
                return {}
            result = {}
            for _, r in df.iterrows():
                raw_code = r.get("股票代码", "")
                code_key = str(int(raw_code)) if isinstance(raw_code, (int, float)) else str(raw_code)
                inflow = _parse_amount(str(r.get("流入资金", "0") or "0"))
                outflow = _parse_amount(str(r.get("流出资金", "0") or "0"))
                net = _parse_amount(str(r.get("净额", "0") or "0"))
                result[code_key] = {
                    "main_net_inflow": net if net != 0 else inflow - outflow,
                    "main_inflow": inflow,
                    "main_outflow": outflow,
                }
            self._bulk_cache = result
            return result
        except Exception:
            return {}

    def _get_moneyflow(self, params: dict) -> dict | None:
        code = params.get("code", "")
        try:
            int(code)
        except (ValueError, TypeError):
            return None
        all_flows = self.get_all_moneyflow()
        return all_flows.get(code)

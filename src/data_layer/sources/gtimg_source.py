"""腾讯 gtimg 数据源适配器 — 实时行情/指数"""
from typing import Any


class GTimgSourceAdapter:
    name = "gtimg"
    capabilities = {"realtime_quote", "realtime_quote_batch", "index"}

    # 单次批量上限 (goal F4.3: 80 只/次)
    BATCH_SIZE = 80

    def is_available(self) -> bool:
        return True

    def get_data(self, data_type: str, params: dict) -> Any:
        if data_type == "realtime_quote":
            return self._get_realtime_quote(params)
        elif data_type == "realtime_quote_batch":
            return self._get_realtime_quote_batch(params)
        elif data_type == "index":
            return self._get_index(params)
        return None

    @staticmethod
    def _to_gt_code(code: str) -> str:
        """6位代码 → gtimg 前缀代码 (sh/sz/bj)"""
        if code.startswith(("0", "3")):
            return f"sz{code}"
        if code.startswith(("4", "8", "9")):
            return f"bj{code}"
        return f"sh{code}"

    @staticmethod
    def _parse_quote_line(code: str, line: str) -> dict | None:
        parts = line.split("~")
        if len(parts) < 34:
            return None
        try:
            return {
                "code": code,
                "name": parts[1],
                "price": float(parts[3]) if parts[3] else 0,
                "change_pct": round(float(parts[32]), 2) if parts[32] else 0.0,
                "volume": float(parts[6]) if parts[6] else 0,
                "amount": float(parts[37]) if len(parts) > 37 and parts[37] else 0,
                "high": float(parts[33]) if len(parts) > 33 and parts[33] else 0,
                "low": float(parts[34]) if len(parts) > 34 and parts[34] else 0,
                "open": float(parts[5]) if parts[5] else 0,
                "pre_close": float(parts[4]) if parts[4] else 0,
            }
        except (ValueError, IndexError):
            return None

    def _get_realtime_quote_batch(self, params: dict) -> list[dict]:
        """批量实时报价 — codes 列表, 每批最多 BATCH_SIZE 只, 一次 HTTP 请求"""
        codes = params.get("codes", [])
        if not codes:
            return []
        results: list[dict] = []
        try:
            import requests
            for i in range(0, len(codes), self.BATCH_SIZE):
                batch = codes[i:i + self.BATCH_SIZE]
                q = ",".join(self._to_gt_code(c) for c in batch)
                resp = requests.get(f"http://qt.gtimg.cn/q={q}", timeout=10)
                resp.encoding = "gbk"
                # 每行形如 v_sz000001="51~平安银行~000001~...";
                for line in resp.text.strip().split("\n"):
                    line = line.strip()
                    if not line or "~" not in line:
                        continue
                    # 从 v_xxNNNNNN=" 提取 6 位代码
                    try:
                        payload = line.split("=", 1)[1].strip().strip('";')
                        code = payload.split("~")[2] if len(payload.split("~")) > 2 else ""
                    except (IndexError, ValueError):
                        continue
                    quote = self._parse_quote_line(code, payload)
                    if quote and quote["price"] > 0:
                        results.append(quote)
        except Exception:
            pass
        return results

    def _get_realtime_quote(self, params: dict) -> dict | None:
        code = params.get("code", "")
        gt_code = f"sz{code}" if code.startswith(("0", "3")) else f"sh{code}"
        try:
            import requests
            resp = requests.get(f"http://qt.gtimg.cn/q={gt_code}", timeout=10)
            resp.encoding = "gbk"
            line = resp.text.strip()
            if not line or "~" not in line:
                return None
            parts = line.split("~")
            if len(parts) < 34:
                return None
            return {
                "code": code,
                "name": parts[1],
                "price": float(parts[3]) if parts[3] else 0,
                "change_pct": round(float(parts[32]), 2) if parts[32] else 0.0,
                "volume": float(parts[6]) if parts[6] else 0,
                "amount": float(parts[37]) if len(parts) > 37 and parts[37] else 0,
                "high": float(parts[33]) if len(parts) > 33 and parts[33] else 0,
                "low": float(parts[34]) if len(parts) > 34 and parts[34] else 0,
                "open": float(parts[5]) if parts[5] else 0,
                "pre_close": float(parts[4]) if parts[4] else 0,
            }
        except Exception:
            return None

    def _get_index(self, params: dict) -> list[dict]:
        codes = params.get("codes", "sh000001,sz399001,sz399006,sh000688,sh000300")
        try:
            import requests
            resp = requests.get(f"http://qt.gtimg.cn/q={codes}", timeout=10)
            resp.encoding = "gbk"
            result = []
            for line in resp.text.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("~")
                if len(parts) < 34:
                    continue
                try:
                    name_map = {"上证指数": "上证", "深证成指": "深证", "创业板指": "创业板",
                                "科创50": "科创50", "沪深300": "沪深300"}
                    n = parts[1]
                    result.append({
                        "name": name_map.get(n, n),
                        "price": float(parts[3]),
                        "change_pct": round(float(parts[32]), 2),
                    })
                except (ValueError, IndexError):
                    pass
            return result
        except Exception:
            return []

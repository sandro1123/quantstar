"""AData 数据源适配器 — 免费多源保护架构 (卡#20 第一段, 统筹 sm-64aaee3df477431b)

侦察定谳 (8 发探针, 沙箱零污染):
- lhb: Hot().list_a_list_daily(report_date=T-1) 真数据✅ — fuyao 主源的即时对冲备源
  (fuyao lhb_daily 滞后 09-04)。显式 T-1: 东财数据中心入库滞后, 默认当天会拿空。
- popularity: b 案单腿 (统筹 sm-3f5181e5931d4ae8 裁定) — emappdata 第一腿
  (rank+hisRc), 价格列缺省诚实化。push2 域被墙死 (http_code=000 环境级), 不叠腿。
- concept_hot: Hot().hot_concept_20_ths() 真数据✅ (20 行热门概念)。
- mine_clearance: MineClearance().mine_clearance_tdx(code) 个股风险档案
  (非解禁日历) — L1 防雷原料, 逐股一调。
- 北向 (个股级停披全 0) / 两融 (API 空): 不入册 (统筹 09-09 定谳)。
"""
from common.logging import get_logger

logger = get_logger("data.sources.adata")


class SourceContractError(ValueError):
    """源契约不满足 (如 report_date 不可得)。

    与普通网络/解析异常区别对待: 契约错误必须**穿透** get_data 的宽 except,
    让上层 (fusion / 管线) 看到病因并如实报 failed (铁律 #12 降级不得静默)。
    """


def _import_adata():
    try:
        from adata.sentiment import Hot, MineClearance
        return Hot, MineClearance
    except ImportError:
        return None, None


class ADataSourceAdapter:
    """AData 免费源 — 情绪/龙虎榜对冲族"""

    def __init__(self):
        self._hot = None
        self._mine = None
        self._init_ok = False

    @property
    def name(self) -> str:
        return "adata"

    @property
    def capabilities(self) -> set[str]:
        return {"lhb", "popularity", "concept_hot", "mine_clearance"}

    def _ensure(self) -> bool:
        """惰性初始化 — adata 未安装时 is_available 已拦, 这里只兜底。"""
        if self._init_ok:
            return True
        Hot, MineClearance = _import_adata()
        if Hot is None:
            return False
        self._hot = Hot()
        self._mine = MineClearance()
        self._init_ok = True
        return True

    def is_available(self) -> bool:
        """门控: adata 可导入 + popularity 第一腿端点显式探活 (b 案单腿单点须知死活)。

        探活只查 emappdata (第一腿); push2 (第二腿) 已裁定不依赖, 不探。
        探活结果进程级缓存 10 分钟, 避免 registry 每次排序都打网络。
        """
        import time
        now = time.time()
        if getattr(self, "_avail_cached", None) is not None and now - self._avail_ts < 600:
            return self._avail_cached
        Hot, _ = _import_adata()
        if Hot is None:
            self._avail_cached, self._avail_ts = False, now
            return False
        ok = self._probe_popularity_endpoint()
        self._avail_cached, self._avail_ts = ok, now
        return ok

    @staticmethod
    def _probe_popularity_endpoint() -> bool:
        """emappdata 第一腿探活 (轻量 POST, 与 adata 同端点)。"""
        try:
            import requests
            r = requests.post(
                "https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
                json={"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
                      "marketType": "", "pageNo": 1, "pageSize": 5},
                timeout=8)
            return r.status_code == 200 and bool(r.json().get("data"))
        except Exception as e:
            logger.debug(f"adata popularity probe fail: {e}")
            return False

    def get_data(self, data_type: str, params: dict):
        if not self._ensure():
            return None
        try:
            if data_type == "lhb":
                return self._get_lhb(params)
            if data_type == "popularity":
                return self._get_popularity(params)
            if data_type == "concept_hot":
                return self._get_concept_hot(params)
            if data_type == "mine_clearance":
                return self._get_mine_clearance(params)
        except SourceContractError:
            raise  # 契约错误穿透: 上层须看到病因 (铁律 #12 降级不得静默)
        except Exception as e:
            logger.warning(f"adata get_data({data_type}) 失败: {e}")
            return None
        return None

    # ---- lhb 备源 (report_date 必须显式, 禁静默回退 T-1) ----
    def _get_lhb(self, params: dict) -> list[dict]:
        """report_date 必须显式传 (params['report_date'])。

        卡② (2026-09-15 全零事故): 旧实现缺省回退 T-1(昨天) → 拉 T-1 数据以
        当日日期落库, 造成 09-15 的 72 行全零占位 (与 09-14 码集 100% 重合)。
        实测 (09-16): list_a_list_daily(report_date=D) 返回行 trade_date 恰为 D,
        故 T-1 回退不仅错且无必要。缺 report_date → 报错 (铁律 #12 降级不得静默)。

        字段映射与 lhb 腿契约对齐 (handler 读 buy_in/turnover/capitalization);
        旧名 net_amount/buy_amount/sell_amount 读不到 → 金额全零。
        """
        rd = params.get("report_date")
        if not rd:
            raise SourceContractError(
                "adata lhb: report_date 不可得 (params 未传) — 拒绝静默回退 T-1 "
                "(会造成 T-1 数据以当日日期落库的全零占位事故)")
        df = self._hot.list_a_list_daily(report_date=rd)
        if df is None or df.empty:
            return []
        return [
            {"code": str(r["stock_code"]), "trade_date": str(r["trade_date"])[:10],
             "close": _f(r.get("close")), "change_pct": _f(r.get("change_cpt")),
             "buy_in": _f(r.get("a_net_amount")), "turnover": _f(r.get("a_buy_amount")),
             "capitalization": _f(r.get("a_sell_amount")), "reason": str(r.get("reason", "")),
             "source": "adata"}
            for _, r in df.iterrows()
        ]

    # ---- popularity b 案单腿 ----
    def _get_popularity(self, params: dict) -> list[dict]:
        """b 案: rank + hisRc (历史排名), 价格列缺省 None (诚实降级)。

        不走 adata 的 pop_rank_100_east (第二腿 push2 被墙死),
        自直拉第一腿 emappdata — sc 形如 SH600869 → code=600869。
        """
        import requests
        r = requests.post(
            "https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
            json={"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
                  "marketType": "", "pageNo": 1, "pageSize": params.get("limit", 100)},
            timeout=10)
        data = (r.json() or {}).get("data") or []
        out = []
        for item in data:
            sc = str(item.get("sc", ""))
            code = sc[2:] if sc[:2] in ("SH", "SZ") else sc
            out.append({"code": code, "rank": int(item.get("rk", 0)),
                        "his_rank": _f(item.get("hisRc")),
                        "price": None, "change_pct": None,  # b 案诚实缺省
                        "source": "adata"})
        return out

    # ---- concept_hot ----
    def _get_concept_hot(self, params: dict) -> list[dict]:
        df = self._hot.hot_concept_20_ths(plate_type=params.get("plate_type", 1))
        if df is None or df.empty:
            return []
        return [
            {"rank": int(r["rank"]), "concept_code": str(r["concept_code"]),
             "concept_name": str(r["concept_name"]), "change_pct": _f(r.get("change_pct")),
             "hot_value": _f(r.get("hot_value")), "hot_tag": str(r.get("hot_tag", "")),
             "source": "adata"}
            for _, r in df.iterrows()
        ]

    # ---- mine_clearance (风险档案) ----
    def _get_mine_clearance(self, params: dict) -> list[dict]:
        """params['code'] 逐股; 返回风险档案行 (score/f_type/s_type/t_type/reason)。"""
        code = params.get("code", "")
        if not code:
            return []
        df = self._mine.mine_clearance_tdx(code)
        if df is None or df.empty:
            return []
        return [
            {"code": str(r.get("stock_code", code)), "short_name": str(r.get("short_name", "")),
             "score": _f(r.get("score")), "f_type": str(r.get("f_type", "")),
             "s_type": str(r.get("s_type", "")), "t_type": str(r.get("t_type", "")),
             "reason": str(r.get("reason", "")), "source": "adata"}
            for _, r in df.iterrows()
        ]


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

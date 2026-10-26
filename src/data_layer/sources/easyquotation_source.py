"""EasyQuotation 新浪源适配器 — 当日 K 线快照接力 (akshare fallback 修法卡 09-28)。

派单 (sm-1f78f9a5389b49e0): akshare 空 → easyquotation(sina) 接力, 不静默断流。
底线: 不绕东财反爬 (D-43) — 本源走 sina 端点, 与东财无涉, 非 WAF 绕行。

定位: 逐股 kline 链 tushare(1) → akshare_sina(2) → **easyquotation(2)** → baostock(3)
的第三位。easyquotation 无历史日线接口, 只补「当日」一格 (09-25 塌的正是当日格);
历史段仍靠链上 tushare/baostock。force_ipv4 局部补丁 (with 边界, 不全局污染)。
"""
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from data_layer._models import Kline


@contextmanager
def _force_ipv4_ctx(enabled: bool):
    """[已拔除: 多线程下全局污染 socket 层级]

    原实现改 urllib3.util.connection.allowed_gai_family 全局属性
    多线程下还原锚读到已被污染的值 -> socket 层永久固化
    -> 全部数据源 (tushare/akshare_sina/akshare/mootdx/baostock/zzshare)
    报 AddressFamily object is not callable -> A2 Day-2 9 表悬崖。

    判例=多线程下「临时改全局属性再还原」不是线程安全的。
    easyquotation 接的是 sina, LA 出口 IPv4 已通, 无需强改 socket 层。
    此函数保留签名兼容调用点, 但不再碰全局属性。"""
    yield


class EasyQuotationSourceAdapter:
    """EasyQuotation 数据源适配器 (sina 端点, 当日快照)。"""

    name = "easyquotation"
    capabilities = {"kline"}

    def __init__(self, force_ipv4: bool = True):
        self.force_ipv4 = force_ipv4
        # 双标记之一 (另一个是 akshare 侧 em_downgrades): 失败打标成功清标,
        # 与 em_downgrades 同构 — orchestrator 可读后随 PipelineResult.metadata
        # 落 pipeline_runs (不静默断流的落点)。
        self.easyquotation_failure: dict[str, str] = {}

    def is_available(self) -> bool:
        try:
            import easyquotation  # noqa: F401
            return True
        except ImportError:
            return False

    def get_data(self, data_type: str, params: dict) -> Any:
        if data_type != "kline":
            return None
        if not self.is_available():
            return None
        code = params.get("code", "")
        try:
            with _force_ipv4_ctx(self.force_ipv4):
                rows = self._fetch_sina(code)
            # 卡E-2 (2026-10-07, spec EQ_WIRE 刀2): 越窗行不落库 —
            # sina 实时快照的行内 date = 快照时刻最近交易日, 假日/盘前调
            # 返回的是「最近交易日」行, 落库会 PK(code,date) 撞既有行同值覆盖
            # (10-07 实测: 5224 只 eq 行全撞 09-30, 零痕不可观测)。
            # 请求窗 end_date ≠ 行内 date → 诚实空窗, 不假值 (铁律#12)。
            end_date = (params.get("end_date") or "")[:10]
            out_of_window = [k for k in rows if end_date and k.date != end_date]
            if out_of_window:
                rows = []
            # 成功 → 清标 (自动恢复语义同 em_downgrades)
            self.easyquotation_failure.pop(code, None)
            return rows
        except Exception as e:  # noqa: BLE001 — 源失败打标不阻断链
            self.easyquotation_failure[code] = (
                f"{datetime.now().isoformat(timespec='seconds')} {type(e).__name__}: {e}"
            )[:200]
            return []

    def _fetch_sina(self, code: str) -> list[Kline]:
        """easyquotation sina 当日快照 → 单条 Kline (当日格)。"""
        import easyquotation
        q = easyquotation.use("sina")
        symbol = self._symbol(code)
        data = q.real([symbol])
        if not data:
            return []
        row = data.get(code) or data.get(symbol)
        if not row:
            return []
        # F-23 (2026-10-22): 停牌日实时价 now=0 (无成交) —— 同样会落库成 0 价行.
        # 与 akshare_sina 对齐: 0 价行直接跳过. 过滤放在 _fetch_sina 层,
        # **不改 _to_kline 签名** (它是 staticmethod 且被既有测试直接调用,
        #  判例 #150 要求测试跨对象边界真调被测对象).
        try:
            now = float(row.get("now", 0) or 0)
        except (TypeError, ValueError):
            now = 0.0
        if now <= 0:
            return []
        return [self._to_kline(code, row)]

    @staticmethod
    def _symbol(code: str) -> str:
        if code.startswith(("92", "43", "83", "87", "4", "8")):
            return f"bj{code}"
        return f"sh{code}" if code.startswith("6") else f"sz{code}"

    @staticmethod
    def _to_kline(code: str, r: dict) -> Kline:
        def _f(key):
            try:
                return float(r.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        now = _f("now")
        open_p = _f("open")
        pre_close = _f("close")  # sina 字段 close = 昨收
        if pre_close > 0:
            change_pct = round((now - pre_close) / pre_close * 100, 2)
        elif open_p > 0:
            change_pct = round((now - open_p) / open_p * 100, 2)
        else:
            change_pct = 0.0
        return Kline(
            code=code,
            date=str(r.get("date", ""))[:10],
            open=open_p, high=_f("high"), low=_f("low"), close=now,
            # easyquotation 0.7.7 sina.py: turnover=int(stock[9])=成交量(股), volume=float(stock[10])=成交额(元)
            # ⇒ 键名语义与名字相反; 落库须对调, 否则量/额两列倒置 (判例 #186)
            volume=_f("turnover"), amount=_f("volume"),
            pre_close=pre_close, change_pct=change_pct,
        )

"""mootdx 数据源适配器 — 通达信TCP协议（财务/股票列表/板块/除权除息）

DL-27 (2026-08-23): 修复连通性 + 能力接入。
1. bestip=True + timeout=15: 原 Quotes.factory(market="std") 无 bestip 无 timeout,
   行情服务器超时导致 29 个方法仅 3 个可用 (~10% 利用率, F-28)。
   mootdx 是本地 TDX 协议, 无 HTTP 限流 — bestip 重选最优服务器是核心修复。
2. xdxr (除权除息) 接入 get_data: 作为 DL-35 复权因子的第三方交叉验证源,
   从"手工抽查"变成"管道能力"。
3. finance 扩展: ipo_date (可补 stock_info.list_date 缺失) + 流通股本。
4. 注册表可见: 新能力声明在 capabilities, 不 duck-type (前一世 A-2 教训)。
5. 墙钟护栏: mootdx 也是网络调用, 复用 baostock _guarded_call 的 daemon 看门狗
   模式 (不用 concurrent.futures, 其非 daemon 线程在 atexit 被 join → P0-7)。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("data.mootdx")

# DL-27: 墙钟超时 (秒)。mootdx TDX 协议正常响应 <2s, 15s = 7 倍余量。
_MOOTDX_TIMEOUT_S = 15.0
# 09-14 病理: bestip 重选 = 104 台 HQ 逐台 connect 串行, 单台 0.7s 理论上界
# ~73s; 正常网络下秒级完成。init 墙钟给 90s (上界+余量), 超时判挂死。
_MOOTDX_CLIENT_INIT_TIMEOUT_S = 90.0


class MootdxTimeoutError(Exception):
    """mootdx 调用墙钟超时 (DL-27) — 向上传播, 不吞成空结果。"""


class MootdxSourceAdapter:
    name = "mootdx"
    # DL-27: 新增 adjust_factor (xdxr 交叉验证) + stock_info (ipo_date/流通股本)。
    # capabilities 必须与 get_data 分发键一致 (A-2 教训: 漏了就是死值)。
    capabilities = {"financial", "stock_list", "concept", "adjust_factor", "stock_info"}

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            from mootdx.quotes import Quotes
            # DL-27: bestip=True 重选最优服务器 + timeout=15 防无限阻塞。
            # 原实现无 bestip 无 timeout → 行情服务器超时 (F-28 根因)。
            # 09-14 病理补墙钟 (adjust_factor 18:30 挂死 32min 案): factory 的
            # bestip 重选 = server.py 对 104 台 HQ 服务器逐台 socket.connect
            # 串行 (单台 settimeout=0.7s 但内核级挂起可无限期), factory 整体
            # 在 DL-27 _guarded_call 护栏**外** → 进程僵死。同款 daemon 线程
            # 护栏移植 (tushare 墙钟修 643e0e6c 先例): init 也入墙钟, 超时抛
            # MootdxTimeoutError 且 _client 保持 None (下次全新 factory, 不留
            # 半初始化死 client)。
            holder: list = []

            def _factory():
                try:
                    holder.append(Quotes.factory(
                        market="std", bestip=True, timeout=int(_MOOTDX_TIMEOUT_S)))
                except Exception as e:  # noqa: BLE001 — 透传给调用方
                    holder.append(e)

            t = threading.Thread(target=_factory, name="mootdx-init-guard", daemon=True)
            t.start()
            t.join(timeout=_MOOTDX_CLIENT_INIT_TIMEOUT_S)
            if t.is_alive() or not holder:
                logger.error(
                    "mootdx client init 墙钟超时 (%.0fs) — bestip 重选挂死 "
                    "(104 台 HQ 串行), 放弃本次 init", _MOOTDX_CLIENT_INIT_TIMEOUT_S)
                raise MootdxTimeoutError(
                    f"mootdx client init 墙钟超时 ({_MOOTDX_CLIENT_INIT_TIMEOUT_S:.0f}s)")
            got = holder[0]
            if isinstance(got, Exception):
                raise got
            self._client = got
        return self._client

    def is_available(self) -> bool:
        try:
            import mootdx  # noqa: F401
            return True
        except ImportError:
            return False

    # ---- 墙钟护栏 (DL-27, 复用 baostock daemon 看门狗模式) ----
    def _guarded_call(self, fn, timeout_s: float = _MOOTDX_TIMEOUT_S):
        """在墙钟护栏内执行 fn()。超时抛 MootdxTimeoutError。

        daemon 看门狗到点设标记; fn 内部无法被中断 (TDX C 层),
        但 mootdx 自身 timeout 参数会在 socket 层超时抛异常。
        看门狗是兜底: 若 mootdx timeout 失效, 至少不永久挂死调用方。
        """
        result = [None]
        error = [None]
        done = threading.Event()

        def _worker():
            try:
                result[0] = fn()
            except Exception as e:
                error[0] = e
            finally:
                done.set()

        t = threading.Thread(target=_worker, name="mootdx-guard", daemon=True)
        t.start()
        if not done.wait(timeout_s):
            raise MootdxTimeoutError(f"mootdx 调用墙钟超时 ({timeout_s:.0f}s)")
        if error[0] is not None:
            raise error[0]
        return result[0]

    def get_data(self, data_type: str, params: dict) -> Any:
        client = self._get_client()
        if client is None:
            return None
        if data_type == "financial":
            return self._get_financial(client, params)
        elif data_type == "stock_list":
            return self._get_stock_list(client)
        elif data_type == "concept":
            return self._get_concept(client)
        elif data_type == "adjust_factor":
            # DL-27/DL-35: xdxr 除权除息 → 复权因子交叉验证源
            return self._get_adjust_factor(client, params)
        elif data_type == "stock_info":
            # DL-27: ipo_date + 流通股本 (补 stock_info.list_date 缺失)
            return self._get_stock_info(client, params)
        return None

    def _get_market(self, code: str) -> int:
        return 0 if code.startswith(("0", "3")) else 1

    def _get_financial(self, client, params: dict) -> dict | None:
        code = params.get("code", "")
        try:
            df = self._guarded_call(lambda: client.finance(code))
            if df is None or df.empty:
                return None
            row = df.iloc[0] if hasattr(df, 'iloc') else df
            # DL-27: mootdx finance 列名为中文 (见 mootdx/financial/columns.py)。
            # 原实现用拼音键 (meigushouyi 等) 全部 .get 落空返回 0 — 已修正。
            return {
                "code": code,
                "eps": float(row.get("基本每股收益", 0) or 0),
                "roe": float(row.get("净资产收益率", 0) or 0),
                "revenue": float(row.get("营业收入", 0) or 0),
                "profit": float(row.get("净利润", 0) or 0),
                "total_assets": float(row.get("资产总计", 0) or 0),
                "total_liabilities": float(row.get("负债合计", 0) or 0),
                "net_assets": float(row.get("所有者权益（或股东权益）合计", 0) or 0),
            }
        except MootdxTimeoutError:
            logger.error(f"mootdx financial {code} 墙钟超时 (DL-27)")
            raise
        except Exception:
            return None

    def _get_stock_list(self, client) -> list[dict]:
        try:
            df = self._guarded_call(lambda: client.stock_all())
            if df is None or df.empty:
                return []
            result = []
            for _, r in df.iterrows():
                code = str(r.get("code", ""))
                if code.startswith(("0", "3", "6")):
                    result.append({"code": code, "name": str(r.get("name", ""))})
            return result
        except MootdxTimeoutError:
            logger.error("mootdx stock_list 墙钟超时 (DL-27)")
            raise
        except Exception:
            return []

    def _get_concept(self, client) -> list[dict]:
        try:
            df = self._guarded_call(lambda: client.block())
            if df is None or df.empty:
                return []
            return [{"block_name": r["blockname"], "block_type": r["block_type"], "code": r["code"]}
                    for _, r in df.iterrows()]
        except MootdxTimeoutError:
            logger.error("mootdx concept 墙钟超时 (DL-27)")
            raise
        except Exception:
            return []

    def _get_adjust_factor(self, client, params: dict) -> list[dict]:
        """DL-27/DL-35: xdxr 全历史 → 归一化为 adjust_factor 契约。

        返回 [{code, ex_date, fenhong, songzhuangu, peigujia, category}]。
        作为 baostock 复权因子的第三方交叉验证源 (前一世手工抽查已验证:
        茅台 mootdx fenhong 280.24/10股 = 28.02 元/股, 与 baostock 一致)。
        """
        code = params.get("code", "")
        if not code:
            return []
        try:
            df = self._guarded_call(lambda: client.xdxr(code))
            if df is None or df.empty:
                return []
            records = []
            for _, r in df.iterrows():
                y = int(r.get("year", 0))
                m = int(r.get("month", 0))
                d = int(r.get("day", 0))
                if y < 1990:
                    continue
                records.append({
                    "code": code,
                    "ex_date": f"{y:04d}-{m:02d}-{d:02d}",
                    "fenhong": float(r.get("fenhong", 0) or 0),
                    "songzhuangu": float(r.get("songzhuangu", 0) or 0),
                    "peigujia": float(r.get("peigujia", 0) or 0),
                    "category": int(r.get("category", 0)),
                })
            return records
        except MootdxTimeoutError:
            logger.error(f"mootdx xdxr {code} 墙钟超时 (DL-27)")
            raise
        except Exception:
            return []

    def _get_stock_info(self, client, params: dict) -> dict | None:
        """DL-27: 流通股本 (补 stock_info 数据)。

        注意: mootdx finance() 列名为中文 (见 mootdx/financial/columns.py),
        无 IPO 日期列。ipo_date 需其他源 (tushare stock_basic) 补充,
        本方法仅提供流通股本。
        """
        code = params.get("code", "")
        if not code:
            return None
        try:
            df = self._guarded_call(lambda: client.finance(code))
            if df is None or df.empty:
                return None
            row = df.iloc[0] if hasattr(df, 'iloc') else df
            return {
                "code": code,
                # mootdx finance 列名为中文: '已上市流通A股' = 流通股本
                "float_shares": float(row.get("已上市流通A股", 0) or 0),
                "total_shares": float(row.get("总股本", 0) or 0),
            }
        except MootdxTimeoutError:
            logger.error(f"mootdx stock_info {code} 墙钟超时 (DL-27)")
            raise
        except Exception:
            return None

    def get_xdxr(self, code: str, ex_date: str) -> dict | None:
        """除权除息事件查询 (DL-35 交叉验证用) — mootdx xdxr 全历史中匹配 ex_date。

        返回 {fenhong(元/10股), songzhuangu(每10股送转), peigujia, ...} 或 None。
        DL-27: 加墙钟护栏。
        """
        try:
            client = self._get_client()
            if client is None:
                return None
            df = self._guarded_call(lambda: client.xdxr(code))
            if df is None or df.empty:
                return None
            y, m, d = int(ex_date[:4]), int(ex_date[5:7]), int(ex_date[8:10])
            for _, r in df.iterrows():
                if (int(r.get("year", 0)) == y and int(r.get("month", 0)) == m
                        and int(r.get("day", 0)) == d):
                    return {
                        "fenhong": float(r.get("fenhong", 0) or 0),
                        "songzhuangu": float(r.get("songzhuangu", 0) or 0),
                        "peigujia": float(r.get("peigujia", 0) or 0),
                        "category": int(r.get("category", 0)),
                        "name": str(r.get("name", "")),
                    }
            return None
        except MootdxTimeoutError:
            logger.error(f"mootdx get_xdxr {code} 墙钟超时 (DL-27)")
            return None  # 交叉验证用, 超时不阻断主流程
        except Exception:
            return None

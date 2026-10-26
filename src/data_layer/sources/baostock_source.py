"""BaoStock 数据源适配器 — K线/批量K线/估值/复权因子/分红 (D-39 恢复接入)

DL-09 (2026-08-18) 曾以"登录失败 10001011 黑名单"移除本适配器;
2026-08-22 复测证伪 (login error_code=0 success, 0.06s), 决策账本 D-39 定案恢复。
推测原次失败撞上 Tushare 机房故障期 (08-18~08-23) 网络异常。

关键能力 (数据源能力全盘点-2026-08-22 §一):
- query_daily_history_k_AStock(date): 按日全市场批量 K 线, 5211 行/15.4s, 18 字段,
  adjustflag='3' 明确不复权 → tushare 单点故障的备源 (DL-25)。
- query_adjust_factor / query_daily_adjust_factor: 复权因子 (DL-35)。
- query_dividend_data: 分红送转全流程 14 字段 (DL-35)。

口径 (务必注意, 见盘点文档 §七 单位陷阱):
- volume 单位=股, amount 单位=元, 与库内一致, **无需转换**
  (切勿照抄 tushare_source.py 的 ×100/×1000)。
- K 线取 adjustflag='3' (不复权/原始价), 与库内实际存储口径一致 (F-21/D-40:
  库内是未复权价)。旧适配器曾用 adjustflag='2' 前复权, 是混源病灶, 本次改原始价。

生命周期: baostock 是有状态连接 (login/logout), 与无状态源不同。
本适配器用实例级登录守卫 + 全局锁串行化 (baostock 单 socket 非线程安全),
不在每次取数时反复登录; 查询遇连接错误时自动重登一次。

DL-25-guard (2026-08-22): 结果集迭代墙钟护栏。
事故: 探针死在 `while rs.next()` 7h20m, 94.9% CPU, `socket.setdefaulttimeout(20)`
无效。根因: baostock socketutil.send_msg 是 `while True: recv()` —— 对端关闭后
recv 永久返回 b"", 终止符 `<![CDATA[]]>\n` 永不匹配 → 空转 (busy-spin);
setdefaulttimeout 只能让"阻塞等包"的 recv 抛 socket.timeout, 止不住空转,
且控制流困在单次 next() 内部, 调用方的逐行检查也永远没机会执行。
护栏设计 (统筹裁定方案 a —— 不用 concurrent.futures, 其非 daemon 线程在 atexit
被 join, 正是 P0-7 ghost 挂 6h47m 的成因):
1. 查询+迭代在**同一锁段内原子执行** (原实现查询持锁/迭代释锁 —— 单 socket 非
   线程安全, 释锁间隙另一线程发新查询会交错翻页错乱); 超时后锁由 with 语句
   保证释放, 不死锁 (统筹 DL-25-guard 步骤 6);
2. daemon 看门狗线程到点强制 close baostock 全局 socket (context.default_socket)
   → 正在 send_msg 里空转的 recv 立即抛 OSError 逃出 C 层循环 —— 兜住空转型挂死;
   daemon=True 保证进程 atexit 不 join 它, 不制造新的 P0-7;
3. 逐行 time.monotonic() 墙钟检查 —— 兜住"慢滴"型挂死 (每行都慢, 累积超预算);
4. 超时抛 SourceTimeoutError 向上传播 (不吞成 []), 管线记录 errors 并降级到
   下一个源 (铁律 #12 可观测不可静默);
5. 超时后登录标记失效 + 强关 socket, 下次调用重新登录 (挂死后 socket 状态不可信)。
测试缝: _query_with_relogin 仍是可 patch 的查询入口 (test_adjust_factor.py 依赖);
生产路径行已暂存 result._qs_rows (list), _drain_guarded 直接取; patch 的假句柄
无 _qs_rows 时回落锁内护栏迭代, 两条路径都带护栏。
超时值: env QS_BAOSTOCK_ITER_TIMEOUT_S > config data.baostock_iter_timeout_s > 120s
(正常批量 15.4s 的 8 倍余量)。不模块级 cfg_get: config_layer 单例污染 (CF-01)。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Any

logger = logging.getLogger("data.baostock")

# baostock 底层 socket.connect 无超时, 服务器不可达时会无限阻塞 → 连接前设全局超时。
_CONNECT_TIMEOUT_S = 5
# 全局锁: baostock 单 socket 非线程安全, 串行化所有登录/查询/迭代 (备源, 串行代价可接受)
_BS_LOCK = threading.Lock()

# DL-25-guard: 查询+结果集迭代的墙钟超时默认值 (秒)。正常批量 K 线 15.4s, 120s = 8 倍余量。
_DEFAULT_ITER_TIMEOUT_S = 120.0


class SourceTimeoutError(Exception):
    """数据源墙钟超时 (DL-25-guard) — 必须向上传播, 不得吞成空结果静默降级。"""


def _iter_timeout_s() -> float:
    """读取墙钟超时 (秒)。env 覆盖 > config > 默认 120s。延迟到调用时读 (CF-01)。"""
    env = os.environ.get("QS_BAOSTOCK_ITER_TIMEOUT_S")
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            logger.warning(f"QS_BAOSTOCK_ITER_TIMEOUT_S 非法值 {env!r}, 忽略")
    try:
        from config_layer import cfg_get
        v = float(cfg_get("data.baostock_iter_timeout_s", _DEFAULT_ITER_TIMEOUT_S))
        return v if v > 0 else _DEFAULT_ITER_TIMEOUT_S
    except Exception:  # noqa: BLE001 — 配置层任何异常 (含 CF-01) 都回落默认, 不阻断取数
        return _DEFAULT_ITER_TIMEOUT_S


class BaoStockSourceAdapter:
    name = "baostock"
    # DL-35 开放复权元数据 (adjust_factor/dividend); DL-25 追加 kline_bulk/kline。
    # 注意: SourceRegistry.get_sources_for 先按 `data_type in capabilities` 过滤,
    # 再按 type_priority 排序 → capabilities 不声明则 __init__ 的 type_priorities 是死值。
    # kline_bulk=2 (tushare 主 1 → baostock 备 2); kline=3 逐股兜底置最后
    # (有状态登录+全局锁, 避免 8-worker 逐股路径锁串行拖慢)。
    capabilities = {
        "adjust_factor",
        "dividend",
        "kline_bulk",
        "kline",
        "trade_status",  # DL-44: 权威停牌判据
        # 丙件 (2026-09-25): 估值逐股兜底源。tushare bulk 限流时主跑采不满
        # (09-16~09-24 实测 ~34%), 顶层补跑腿走本源的 get_valuation_range
        # 逐股补缺口。免 token 无限流, 17:30 后入库稳定 (DL-51 kline 已实证),
        # 实测 0.137s/只 → 3600 只 ~8.2 分钟。
        "valuation",
    }

    def __init__(self) -> None:
        self._logged_in = False

    # ---- 可用性 ----
    @staticmethod
    def is_available() -> bool:
        try:
            import baostock  # noqa: F401
            return True
        except ImportError:
            return False

    # ---- 代码归一化 ----
    @staticmethod
    def _to_baostock_code(code: str) -> str | None:
        raw = str(code).strip().lower()
        if raw.startswith(("sh.", "sz.")):
            return raw
        raw = raw.split(".", 1)[0]
        if raw.startswith(("6", "9")):
            return f"sh.{raw}"
        if raw.startswith(("0", "3")):
            return f"sz.{raw}"
        return None  # 北交所等 baostock 不覆盖

    @staticmethod
    def _plain_code(code: str) -> str:
        return code.split(".", 1)[-1]

    @staticmethod
    def _float(value: Any) -> float:
        try:
            return float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 0.0

    # ---- 登录生命周期 (有状态连接) ----
    def _ensure_login(self) -> bool:
        """已登录则复用; 未登录/失效则登录。调用方须持有 _BS_LOCK。"""
        import baostock as bs

        if self._logged_in:
            return True
        prev = socket.getdefaulttimeout()
        socket.setdefaulttimeout(_CONNECT_TIMEOUT_S)
        try:
            result = bs.login()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"baostock login exception: {e}")
            return False
        finally:
            socket.setdefaulttimeout(prev)
        if result is None or result.error_code != "0":
            logger.warning(
                f"baostock login failed: {getattr(result, 'error_msg', 'unreachable')}")
            self._logged_in = False
            return False
        self._logged_in = True
        return True

    def _logout(self) -> None:
        import baostock as bs
        if self._logged_in:
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass
            self._logged_in = False

    def close(self) -> None:
        """显式释放连接 (供 bootstrap shutdown 调用, 可选)。"""
        with _BS_LOCK:
            self._logout()

    def _force_reset_connection(self) -> None:
        """DL-25-guard: 超时后强制重置连接状态 (看门狗线程/善后路径调用, 不持锁)。

        挂死后的 socket 状态不可信 (可能空转/半开):
        ① 登录标记失效 → 下次调用重新登录;
        ② 强关 baostock 全局 socket (context.default_socket) → 仍在 send_msg
           `while True: recv()` 里空转的调用立即收到 OSError 逃出。
        """
        self._logged_in = False
        try:
            from baostock.common import context
            sock = getattr(context, "default_socket", None)
            if sock is not None:
                # shutdown 先唤醒阻塞在 recv 的线程 (close 单独调用在 Linux 上
                # 不可靠地解除另一线程的阻塞 recv); 再 close 释放 fd。
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass
                context.default_socket = None
        except Exception:  # noqa: BLE001 — baostock 内部结构变化不应阻断善后
            pass

    # ---- 墙钟护栏 (DL-25-guard) ----
    def _guarded_call(self, fn, timeout_s: float):
        """在墙钟护栏内执行 fn(deadline) (调用方须持有 _BS_LOCK)。

        双重防线:
        - daemon 看门狗到点强关 socket → 空转型挂死 (recv 永久返回 b"",
          逐行检查永远没机会执行; 关 socket 使 recv 抛 OSError 逃出);
        - fn 内部按 deadline 做逐行 monotonic 检查 → "慢滴"型挂死。
        超时统一抛 SourceTimeoutError; 到点后的 OSError 视为超时 (看门狗关 socket
        引发的解除瞬间), 避免被当成普通连接错误走静默降级。
        """
        deadline = time.monotonic() + timeout_s
        expired = threading.Event()

        def _watchdog():
            if not expired.wait(timeout_s):
                logger.error(
                    f"baostock 墙钟超时 ({timeout_s:.0f}s) — 看门狗强制关闭连接 (DL-25-guard)")
                self._force_reset_connection()

        wd = threading.Thread(target=_watchdog, name="baostock-guard", daemon=True)
        wd.start()
        try:
            return fn(deadline)
        except SourceTimeoutError:
            self._force_reset_connection()
            raise
        except OSError:
            if time.monotonic() >= deadline:
                self._force_reset_connection()
                raise SourceTimeoutError(
                    f"baostock 查询/迭代墙钟超时 ({timeout_s:.0f}s)") from None
            raise
        finally:
            expired.set()  # 正常返回或异常都解除看门狗

    @staticmethod
    def _drain(result, deadline: float) -> list:
        """完整迭代结果集, 逐行墙钟检查。超时抛 SourceTimeoutError。"""
        rows = []
        while True:
            if time.monotonic() > deadline:
                raise SourceTimeoutError("baostock 结果集迭代墙钟超时 (慢滴)")
            if not result.next():
                break
            rows.append(result.get_row_data())
        return rows

    def _query_with_relogin(self, query_fn):
        """持锁执行查询 + 完整迭代 (带墙钟护栏); 遇连接类错误自动重登一次。

        query_fn: 已登录状态下执行的无参 callable, 返回 baostock ResultData。
        查询与迭代在同一锁段内原子完成 (单 socket 非线程安全, 释锁间隙会被
        并发查询交错翻页); 行暂存于 result._qs_rows (list)。
        超时抛 SourceTimeoutError —— 锁由 with 语句释放, 不会死锁。
        """
        timeout_s = _iter_timeout_s()
        with _BS_LOCK:
            if not self._ensure_login():
                raise ConnectionError("baostock login failed")
            try:
                return self._guarded_call(self._query_and_drain(query_fn), timeout_s)
            except SourceTimeoutError:
                raise  # 看门狗已重置连接, 不走重登, 直接向上传播
            except (ConnectionError, ConnectionResetError, BrokenPipeError, OSError):
                # 连接失效 → 重登一次再试
                self._logged_in = False
                if not self._ensure_login():
                    raise
                return self._guarded_call(self._query_and_drain(query_fn), timeout_s)

    def _query_and_drain(self, query_fn):
        """构造护栏内执行体: 查询 → 迭代 (调用方须持有 _BS_LOCK)。"""

        def _run(deadline):
            result = query_fn()
            if result is None or getattr(result, "error_code", "0") != "0":
                return result  # 错误结果无行可迭代, 交由调用方按 error_code 处理
            result._qs_rows = self._drain(result, deadline)
            return result

        return _run

    def _drain_guarded(self, result) -> list:
        """取结果集全部行。生产路径行已在 _query_with_relogin 锁内带护栏迭代
        完毕, 暂存于 result._qs_rows (list) 直接返回; patch 的假句柄无 _qs_rows
        时回落锁内护栏迭代 (测试缝兼容, 两条路径都带护栏)。

        DL-25-guard: 所有 `while result.next()` 一律经 _drain 单一入口
        (铁律 #11 全覆盖), 禁止在 get_* 里裸写迭代。
        """
        rows = getattr(result, "_qs_rows", None)
        if isinstance(rows, list):
            return rows
        timeout_s = _iter_timeout_s()
        with _BS_LOCK:
            return self._guarded_call(
                lambda deadline: self._drain(result, deadline), timeout_s)

    # ---- 协议入口 ----
    def get_data(self, data_type: str, params: dict) -> Any:
        if data_type == "kline":
            return self.get_kline_range(
                params.get("code", ""),
                params.get("start_date", ""),
                params.get("end_date", ""))
        if data_type in ("valuation",):
            return self.get_valuation_range(
                params.get("code", ""),
                params.get("start_date", ""),
                params.get("end_date", ""))
        if data_type == "stock_list":
            return self.get_stock_list(params.get("date", ""))
        if data_type == "adjust_factor":
            return self.get_adjust_factors(
                params.get("code", ""),
                params.get("start_date", ""),
                params.get("end_date", ""))
        if data_type == "dividend":
            return self.get_dividends(
                params.get("code", ""), params.get("year", 0))
        if data_type == "trade_status":
            return self.get_trade_status(
                params.get("code", ""), params.get("date", ""))
        return None

    # ---- 批量全市场 K 线 (DL-25 核心) ----
    def get_daily_market(self, date: str) -> list[dict]:
        """按日全市场批量 K 线 — query_daily_history_k_AStock 单次调用。

        返回与 tushare get_daily_market 同构的 record dict 列表,
        供 kline 管线批量路径消费。单位: volume=股 amount=元 (不转换)。
        仅覆盖 sh./sz. (无北交所), 故行数下限由调用方 _BULK_MIN_ROWS 把关。
        """
        import baostock as bs

        def _q():
            return bs.query_daily_history_k_AStock(date)

        try:
            result = self._query_with_relogin(_q)
            if result is None or result.error_code != "0":
                logger.warning(
                    f"baostock bulk daily {date} error: "
                    f"{getattr(result, 'error_msg', 'no result')}")
                return []
            rows = self._drain_guarded(result)
        except SourceTimeoutError:
            # DL-25-guard: 超时必须可观测 — 向上传播, 管线记 errors 并降级 (铁律 #12)
            logger.error(f"baostock bulk daily {date} 墙钟超时 (DL-25-guard)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"baostock bulk daily {date} failed: {e}")
            return []
        records: list[dict] = []
        for raw in rows:
            row = dict(zip(result.fields, raw))
            code = self._plain_code(str(row.get("code", "")))
            records.append({
                "code": code,
                "date": str(row.get("date", date)),
                "open": self._float(row.get("open")),
                "high": self._float(row.get("high")),
                "low": self._float(row.get("low")),
                "close": self._float(row.get("close")),
                "volume": self._float(row.get("volume")),   # 股, 不转换
                "amount": self._float(row.get("amount")),   # 元, 不转换
                "pre_close": self._float(row.get("preclose")),
                "change_pct": self._float(row.get("pctChg")),
                "turnover_rate": self._float(row.get("turn")),
            })
        logger.info(f"baostock bulk daily {date}: {len(records)} rows")
        return records

    # ---- 逐只 K 线 (不复权, 与库内口径一致) ----
    def get_kline_range(self, code: str, start_date: str, end_date: str) -> list:
        from data_layer._models import Kline

        bs_code = self._to_baostock_code(code)
        if bs_code is None:
            return []
        import baostock as bs

        def _q():
            return bs.query_history_k_data_plus(
                bs_code,
                "date,code,open,high,low,close,preclose,volume,amount,turn,"
                "tradestatus,pctChg",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="3")  # 3=不复权 (F-21/D-40 库内口径)

        try:
            result = self._query_with_relogin(_q)
            if result is None or result.error_code != "0":
                return []
            rows = self._drain_guarded(result)
        except SourceTimeoutError:
            logger.error(f"baostock kline {code} 墙钟超时 (DL-25-guard)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"baostock kline {code} failed: {e}")
            return []
        out: list[Kline] = []
        for raw in rows:
            row = dict(zip(result.fields, raw))
            if str(row.get("tradestatus", "1")) != "1":
                continue
            out.append(Kline(
                code=self._plain_code(str(row.get("code", code))),
                date=str(row.get("date", "")),
                open=self._float(row.get("open")),
                high=self._float(row.get("high")),
                low=self._float(row.get("low")),
                close=self._float(row.get("close")),
                volume=self._float(row.get("volume")),
                amount=self._float(row.get("amount")),
                pre_close=self._float(row.get("preclose")),
                change_pct=self._float(row.get("pctChg")),
                turnover_rate=self._float(row.get("turn")),
            ))
        return out

    # ---- 估值 (peTTM/pbMRQ/psTTM) ----
    def get_valuation_range(self, code: str, start_date: str, end_date: str) -> list[dict]:
        bs_code = self._to_baostock_code(code)
        if bs_code is None:
            return []
        import baostock as bs

        def _q():
            return bs.query_history_k_data_plus(
                bs_code, "date,code,tradestatus,peTTM,pbMRQ,psTTM,pcfNcfTTM",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="3")

        try:
            result = self._query_with_relogin(_q)
            if result is None or result.error_code != "0":
                return []
            rows = self._drain_guarded(result)
        except SourceTimeoutError:
            logger.error(f"baostock valuation {code} 墙钟超时 (DL-25-guard)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"baostock valuation {code} failed: {e}")
            return []
        records: list[dict] = []
        for raw in rows:
            row = dict(zip(result.fields, raw))
            if str(row.get("tradestatus", "1")) != "1":
                continue
            records.append({
                "code": self._plain_code(str(row.get("code", code))),
                "date": str(row.get("date", "")),
                "pe_ttm": self._float(row.get("peTTM")),
                "pb_mrq": self._float(row.get("pbMRQ")),
                "ps_ttm": self._float(row.get("psTTM")),
                "market_cap": None,
                "total_shares": None,
                "dividend_yield": None,
            })
        return records

    # ---- 股票列表 ----
    def get_stock_list(self, date: str) -> list[dict]:
        import baostock as bs

        def _q():
            return bs.query_all_stock(day=date)

        try:
            result = self._query_with_relogin(_q)
            if result is None or result.error_code != "0":
                return []
            rows = self._drain_guarded(result)
        except SourceTimeoutError:
            logger.error(f"baostock stock_list {date} 墙钟超时 (DL-25-guard)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"baostock stock_list {date} failed: {e}")
            return []
        out: list[dict] = []
        for raw in rows:
            row = dict(zip(result.fields, raw))
            code = self._plain_code(str(row.get("code", "")))
            # 只要 A 股个股 (排除指数 sh.000xxx / 基金等)
            if code and code[0] in ("0", "3", "6"):
                out.append({"code": code, "name": str(row.get("code_name", ""))})
        return out

    # ---- 复权因子 (DL-35) ----
    def get_adjust_factors(self, code: str, start_date: str, end_date: str) -> list[dict]:
        """逐只复权因子 — query_adjust_factor。

        返回 [{code, ex_date, fore_factor, back_factor, adjust_factor, source}]。
        back_factor=后复权因子 (稳定, D-40 采用); fore_factor=前复权因子。
        """
        bs_code = self._to_baostock_code(code)
        if bs_code is None:
            return []
        import baostock as bs

        def _q():
            return bs.query_adjust_factor(bs_code, start_date, end_date)

        try:
            result = self._query_with_relogin(_q)
            if result is None or result.error_code != "0":
                return []
            rows = self._drain_guarded(result)
        except SourceTimeoutError:
            logger.error(f"baostock adjust_factor {code} 墙钟超时 (DL-25-guard)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"baostock adjust_factor {code} failed: {e}")
            return []
        out: list[dict] = []
        for raw in rows:
            row = dict(zip(result.fields, raw))
            ex_date = str(row.get("dividOperateDate", ""))
            if not ex_date:
                continue
            out.append({
                "code": self._plain_code(str(row.get("code", code))),
                "ex_date": ex_date,
                "fore_factor": self._float(row.get("foreAdjustFactor")),
                "back_factor": self._float(row.get("backAdjustFactor")),
                "adjust_factor": self._float(row.get("adjustFactor")),
                "source": "baostock",
            })
        return out

    def get_daily_adjust_factors(self, date: str) -> list[dict]:
        """按日全市场复权因子增量 — query_daily_adjust_factor (每日 ~15 行)。"""
        import baostock as bs

        def _q():
            return bs.query_daily_adjust_factor(date)

        try:
            result = self._query_with_relogin(_q)
            if result is None or result.error_code != "0":
                return []
            rows = self._drain_guarded(result)
        except SourceTimeoutError:
            logger.error(f"baostock daily_adjust_factor {date} 墙钟超时 (DL-25-guard)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"baostock daily_adjust_factor {date} failed: {e}")
            return []
        out: list[dict] = []
        for raw in rows:
            row = dict(zip(result.fields, raw))
            ex_date = str(row.get("dividOperateDate", ""))
            if not ex_date:
                continue
            out.append({
                "code": self._plain_code(str(row.get("code", ""))),
                "ex_date": ex_date,
                "fore_factor": self._float(row.get("foreAdjustFactor")),
                "back_factor": self._float(row.get("backAdjustFactor")),
                "adjust_factor": self._float(row.get("adjustFactor")),
                "source": "baostock",
            })
        return out

    # ---- 分红送转 (DL-35) ----
    def get_dividends(self, code: str, year: int) -> list[dict]:
        """逐只分红 — query_dividend_data(code, year)。

        返回 [{code, ex_date, announce_date, record_date, pay_date,
               cash_dividend(税前/股), stock_dividend(送股/股),
               stock_transfer(转增/股), source}]。
        baostock 现金分红单位=元/股 (dividCashPsBeforeTax)。
        """
        bs_code = self._to_baostock_code(code)
        if bs_code is None or not year:
            return []
        import baostock as bs

        def _q():
            return bs.query_dividend_data(bs_code, int(year))

        try:
            result = self._query_with_relogin(_q)
            if result is None or result.error_code != "0":
                return []
            rows = self._drain_guarded(result)
        except SourceTimeoutError:
            logger.error(f"baostock dividend {code}/{year} 墙钟超时 (DL-25-guard)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"baostock dividend {code}/{year} failed: {e}")
            return []
        out: list[dict] = []
        for raw in rows:
            row = dict(zip(result.fields, raw))
            ex_date = str(row.get("dividOperateDate", ""))
            if not ex_date:
                continue
            out.append({
                "code": self._plain_code(str(row.get("code", code))),
                "ex_date": ex_date,
                "announce_date": str(row.get("dividPlanAnnounceDate", "")),
                "record_date": str(row.get("dividRegistDate", "")),
                "pay_date": str(row.get("dividPayDate", "")),
                "cash_dividend": self._float(row.get("dividCashPsBeforeTax")),
                "stock_dividend": self._float(row.get("dividStocksPs")),
                "stock_transfer": self._float(row.get("dividReserveToStockPs")),
                "source": "baostock",
            })
        return out

    # ---- 交易状态 (DL-44) ----
    def get_trade_status(self, code: str, date: str) -> bool | None:
        """查询个股当日交易状态 (DL-44 权威停牌判据)。

        Returns:
            True  = 当日正常交易 (tradestatus=1)
            False = 当日停牌 (tradestatus=0)
            None  = 无法判定 (baostock 无该股/当日数据/查询失败)
        """
        bs_code = self._to_baostock_code(code)
        if bs_code is None:
            return None
        import baostock as bs

        def _q():
            return bs.query_history_k_data_plus(
                bs_code, "date,code,tradestatus",
                start_date=date, end_date=date,
                frequency="d", adjustflag="3")

        try:
            result = self._query_with_relogin(_q)
            if result is None or result.error_code != "0":
                return None
            rows = self._drain_guarded(result)
        except SourceTimeoutError:
            logger.error(f"baostock trade_status {code} {date} 墙钟超时 (DL-25-guard)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"baostock trade_status {code} {date} failed: {e}")
            return None

        if not rows:
            return None
        row = dict(zip(result.fields, rows[0]))
        return str(row.get("tradestatus", "")) == "1"

    def get_daily_trade_status(self, date: str) -> dict[str, bool] | None:
        """按日全市场交易状态批量查询 (DL-44)。

        复用 query_daily_history_k_AStock 的 tradestatus 字段,
        单次调用返回全市场 {code: is_trading} 映射。
        比逐股查询高效 ~5000 倍 (1 次网络调用 vs 5000 次)。

        Returns:
            {code: True/False} 或 None (查询失败/超时)
        """
        import baostock as bs

        def _q():
            return bs.query_daily_history_k_AStock(date)

        try:
            result = self._query_with_relogin(_q)
            if result is None or result.error_code != "0":
                logger.warning(
                    f"baostock daily_trade_status {date} error: "
                    f"{getattr(result, 'error_msg', 'no result')}")
                return None
            rows = self._drain_guarded(result)
        except SourceTimeoutError:
            logger.error(f"baostock daily_trade_status {date} 墙钟超时 (DL-25-guard)")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"baostock daily_trade_status {date} failed: {e}")
            return None

        status_map: dict[str, bool] = {}
        for raw in rows:
            row = dict(zip(result.fields, raw))
            code = self._plain_code(str(row.get("code", "")))
            if code:
                status_map[code] = str(row.get("tradestatus", "1")) == "1"
        logger.info(f"baostock daily_trade_status {date}: {len(status_map)} stocks")
        return status_map

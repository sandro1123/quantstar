"""盘中监控器 — 5 分钟扫描循环"""

import threading
import time

from common.database import Database
from common.logging import get_logger
from common.utils import bj_now
from execution_layer.core.paper_broker import PaperBroker
from execution_layer.intraday.entry_checker import IntradayEntryChecker
from execution_layer.intraday.exit_checker import IntradayExitChecker
from execution_layer.intraday.touch_card import APPROACH_THRESHOLD_PCT, TouchCardPusher
from agent_layer import build_request

logger = get_logger("execution.intraday")

class IntradayMonitor:
    def __init__(self, db, stock_pool, entry_engine=None, exit_engine=None, data_layer=None, agent_layer=None, config=None, touch_card=None):
        self.db = db or Database()
        self.stock_pool = stock_pool
        self.entry_engine = entry_engine
        self.exit_engine = exit_engine
        self.data_layer = data_layer
        self.agent_layer = agent_layer
        self.config = config
        self.interval_s = 300
        self._running = False
        self._thread: threading.Thread | None = None
        dry_run = bool(config.get("execution.dry_run", True)) if config else True
        self.entry_checker = IntradayEntryChecker(self.db, self.stock_pool, self.entry_engine, data_layer=self.data_layer) if self.entry_engine else None
        self.exit_checker = IntradayExitChecker(self.db, self.stock_pool, self.exit_engine, data_layer=self.data_layer) if self.exit_engine else None
        self._pnl_snapshots: list[dict] = []
        self._check_round = 0  # EX-09: intraday_checks 轮次号
        self.paper_broker = PaperBroker(self.db, self.stock_pool, dry_run=dry_run)
        # R2: 触价行动卡推送器 — 默认 None (单测不注入=零推送),
        # 生产由 ExecutionLayer 构造时注入 (cron intraday_scan 路径)。
        # 并联一路: 只额外推卡, 不改 execute_entry/mark_exit_trigger 现有行为。
        self.touch_card: TouchCardPusher | None = touch_card

    def _get_interval_for_time(self, time_str: str) -> int:
        hour_min = time_str.split(":")[0] + ":" + time_str.split(":")[1]
        if "09:30" <= hour_min < "10:00":
            return 120
        elif "10:00" <= hour_min < "14:30":
            return 300
        elif "14:30" <= hour_min < "15:00":
            return 60
        return 300

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("IntradayMonitor started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("IntradayMonitor stopped")

    def _auction_check(self):
        pending = self.stock_pool.get_by_status("pending_entry")
        for item in pending:
            code = item["code"]
            price = self.entry_checker._get_current_price(code) if self.entry_checker else 0
            if price > 0 and item.get("entry_low", 0) <= price <= item.get("entry_high", 0):
                logger.info(f"Auction check: {code} price {price} in range [{item.get('entry_low')}, {item.get('entry_high')}]")

    def _post_close_processing(self):
        logger.info("Post-close processing started")
        active = self.stock_pool.get_by_status("active")
        if self.exit_checker:
            for item in active:
                code = item["code"]
                last_price = item.get("last_price", 0) or 0
                if last_price > 0:
                    result = self.exit_checker.check(code, item)
                    if result:
                        # F4.4: 只标记触发, 结算统一由 daily_update._settle_pending_exits (T+1)
                        self.stock_pool.mark_exit_trigger(
                            code, result.get("price", last_price), result.get("reason", "exit"),
                            result.get("exit_low"))
        exit_pending = self.stock_pool.get_by_status("exit_pending")
        logger.info(f"Post-close: checked {len(active)} active positions, {len(exit_pending)} exit_pending 待 T+1 结算")

    def _loop(self):
        while self._running:
            try:
                now = bj_now()
                time_str = now.split(" ")[1]
                hour = int(time_str.split(":")[0])
                minute = int(time_str.split(":")[1])
                if hour == 9 and minute == 25:
                    self._auction_check()
                if 9 <= hour < 15:
                    self._scan()
                    self.interval_s = self._get_interval_for_time(time_str)
                elif hour >= 15:
                    self._post_close_processing()
                    self._running = False
                    break
            except Exception as e:
                logger.error(f"Intraday scan error: {e}")
            time.sleep(self.interval_s)

    def _push_touch_card(self, method_name: str, *args, **kwargs) -> None:
        """R2: 触价卡推送统一入口 — 一切失败仅 warning, 绝不阻断 _scan 主流程。

        传方法名而非绑定方法: touch_card=None 时取属性本身就会 AttributeError,
        实参求值先于函数体防护, 故在函数体内 getattr。
        """
        if not self.touch_card:
            return
        try:
            getattr(self.touch_card, method_name)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"触价卡推送异常(不影响主流程): {e}")

    def _scan(self):
        active = self.stock_pool.get_by_status("active")
        pending = self.stock_pool.get_by_status("pending_entry")
        self._check_round += 1
        check_rows: list[tuple] = []
        if self.exit_checker:
            for item in active:
                code = item["code"]
                fallback_price = item.get("last_price", 0) or 0
                try:
                    t0 = time.monotonic()
                    result = self.exit_checker.check(code, item)
                    latency = int((time.monotonic() - t0) * 1000)
                except Exception as e:
                    logger.warning(f"[{code}] exit check failed: {e}")
                    check_rows.append(self._check_row(code, "exit", fallback_price, None, "error", str(e), 0))
                    continue
                if result:
                    # F4.4: 盘中只标记触发 (exit_pending + trigger_price/date),
                    # 结算唯一入口 = daily_update._settle_pending_exits (T+1 收盘价)
                    self.stock_pool.mark_exit_trigger(
                        code, result.get("price", 0), result.get("reason", "exit"),
                        result.get("exit_low"))
                    check_rows.append(self._check_row(
                        code, "exit", result.get("price", 0), result.get("exit_low"),
                        "triggered", result.get("reason"), latency))
                    # R2 并联: 止损/退出触发红卡 (mark_exit_trigger 行为不变)
                    self._push_touch_card(
                        "push_stop_trigger", code,
                        result.get("price", 0), result.get("reason", "exit"), item)
                else:
                    check_rows.append(self._check_row(
                        code, "exit", fallback_price, None, "no_action", None, latency))
                    # R2 并联: 止损逼近橙卡 — 未触发但现价距参考卖出线 ≤ 阈值 (D8)。
                    # 现价经 exit_checker 同款取价 (实时优先), 不用滞后的 last_price;
                    # 跨进程冷却去重在 pusher 内, 价格滞留线附近不刷屏。
                    # best-effort: 取价/换算任何异常静默跳过, 不影响 no_action 留痕。
                    try:
                        stop = float(item.get("dynamic_stop_loss") or item.get("stop_loss") or 0)
                        cur = float(self.exit_checker._get_current_price(code) or 0)
                        if stop > 0 and cur > 0:
                            dist_pct = (cur - stop) / stop * 100
                            if 0 <= dist_pct <= APPROACH_THRESHOLD_PCT:
                                self._push_touch_card(
                                    "push_stop_approach", code, cur, stop, item)
                    except (TypeError, ValueError):
                        pass
        if self.entry_checker:
            for item in pending:
                code = item["code"]
                fallback_price = item.get("last_price", 0) or 0
                try:
                    t0 = time.monotonic()
                    result = self.entry_checker.check(code, item)
                    latency = int((time.monotonic() - t0) * 1000)
                except Exception as e:
                    logger.warning(f"[{code}] entry check failed: {e}")
                    check_rows.append(self._check_row(code, "entry", fallback_price, None, "error", str(e), 0))
                    continue
                if result and result.get("blocked"):
                    # R3/D1: 触价但被闸门拦截 → 蓝卡照推 (拦截原因可见, 不静默),
                    # 绝不执行入场; 留痕 status="blocked" (铁律#12 可观测)。
                    check_rows.append(self._check_row(
                        code, "entry", result.get("price", 0), None,
                        "blocked", result.get("reason"), latency))
                    # D1 续卡: 结构化拦截记录落 entry_blocks 台账
                    # (gate=哪道闸, 宁 NULL 不可假值) — best-effort,
                    # 表未建/写失败只 warning, 不影响扫描主流程 (D5 先例)。
                    try:
                        conn = self.db.get_connection()
                        conn.execute(
                            "INSERT INTO entry_blocks "
                            "(block_time, code, gate, detail, price, entry_low, entry_high) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (bj_now(), code, result.get("gate"),
                             result.get("reason"),
                             result.get("price", 0),
                             item.get("entry_low", 0) or 0,
                             item.get("entry_high", 0) or 0))
                        conn.commit()
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"entry_blocks 写入失败 (non-blocking): {e}")
                    self._push_touch_card(
                        "push_entry", code,
                        result.get("price", 0), item,
                        block_reason=result.get("reason"))
                elif result:
                    trade_result = None
                    if self.paper_broker:
                        # R2: execute_entry 的底层即 entry_with_fees (行为不变),
                        # 取 PaperTradeResult 以供触价卡佐证行 (qty/成败原因)。
                        trade_result = self.paper_broker.entry_with_fees(
                            code, result["price"], result.get("reason", "entry"), item)
                    check_rows.append(self._check_row(
                        code, "entry", result.get("price", 0), None,
                        "triggered", result.get("reason"), latency))
                    # R2 并联: 入场触价绿卡 (指引先行, 系统动作降佐证行)
                    self._push_touch_card(
                        "push_entry", code,
                        result.get("price", 0), item, trade_result=trade_result)
                else:
                    check_rows.append(self._check_row(
                        code, "entry", fallback_price, None, "no_action", None, latency))
        self._record_check_rows(check_rows)
        exit_pending = self.stock_pool.get_by_status("exit_pending")
        self._record_pnl_snapshot(active)
        self._check_volatility(active)
        logger.info(f"Intraday scan: {len(active)} active, {len(pending)} pending, {len(exit_pending)} exit_pending (待 T+1 结算)")

    def _check_row(self, code: str, check_type: str, price: float, trigger_price,
                   status: str, detail, latency_ms: int) -> tuple:
        """EX-09: 组装 intraday_checks 一行 (列序与 DDL 一致)"""
        return (bj_now(), f"R{self._check_round}", code, check_type,
                price, trigger_price, status, detail, latency_ms)

    def _record_check_rows(self, rows: list[tuple]):
        """EX-09: 本轮检查记录批量写入 intraday_checks; 失败只 warning, 不影响扫描主流程"""
        if not rows:
            return
        try:
            conn = self.db.get_connection()
            conn.executemany(
                "INSERT INTO intraday_checks (check_time, check_round, code, check_type, "
                "current_price, trigger_price, status, detail, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
        except Exception as e:
            logger.warning(f"intraday_checks 写入失败 ({len(rows)} 行): {e}")

    def _check_volatility(self, active: list[dict]):
        """Detect significant drops from position data (不使用 snapshot_intraday)"""
        if not self.agent_layer:
            return
        try:
            for item in active:
                code = item.get("code", "")
                entry_price = item.get("entry_price", 0) or 0
                last_price = item.get("last_price", 0) or 0
                if entry_price > 0 and last_price > 0:
                    change = (last_price - entry_price) / entry_price * 100
                    if change < -3.0:
                        self._generate_volatility_explanation(code, change, "stock")
        except Exception as e:
            logger.warning(f"Volatility check failed: {e}")

    def _generate_volatility_explanation(self, code: str | None, change: float, vtype: str):
        """Call Agent Layer for volatility explanation"""
        if not self.agent_layer:
            return
        try:
            if vtype == "market":
                # AG-WIRE-1: market_volatility 不在 registry → 改走已注册的
                # market_analysis (AG-11) quick 档; handler 读 date 键, code 用
                # handler 默认 MARKET (None 会炸 write_prediction 的 code NOT NULL);
                # 跌幅信息 (drop_pct/type) 保留不丢上下文。
                from common.utils import bj_date
                req = build_request(
                    task="market_analysis",
                    input={"code": "MARKET", "date": bj_date(),
                           "drop_pct": abs(change), "type": vtype},
                    depth="quick",
                    consumer="execution",
                )
            else:
                req = build_request(
                    task="stock_analysis",
                    input={"code": code, "drop_pct": abs(change), "type": vtype},
                    consumer="execution",
                )
            resp = self.agent_layer.analyze(req)
            if resp and hasattr(resp, 'result'):
                logger.info(f"Volatility explanation generated for {code or 'market'}: {resp.result[:100]}")
        except Exception as e:
            logger.warning(f"Failed to generate volatility explanation: {e}")

    def _record_pnl_snapshot(self, active: list[dict]):
        # P1/M1 计算源修复: None=从未盯市, 不得 or 0 折成"零收益" (宁可 NULL 不可假值)。
        # 只对真实盯市过的仓位求和; 全部未盯市 → 快照落 NULL (不可算 ≠ 平盘)。
        # 真 0 (盯过市且恰 0) 保留计入; 空仓 → 0.0 是真值。
        marked = [item["unrealized_pnl_pct"] for item in active
                  if item.get("unrealized_pnl_pct") is not None]
        if marked:
            total_unrealized = round(sum(marked), 2)
        else:
            # 空仓 = 0.0 是真值 (零持仓即零浮盈); 有仓但从未盯市 = NULL (不可算 ≠ 平盘)
            total_unrealized = 0.0 if not active else None
        snap = {
            "time": bj_now(),
            "unrealized_pnl_pct": total_unrealized,
            "active_count": len(active),
        }
        self._pnl_snapshots.append(snap)
        if len(self._pnl_snapshots) > 200:
            self._pnl_snapshots = self._pnl_snapshots[-200:]
        # D5 通电: 快照落库 — cron 模式每进程仅一条内存快照, 落库才有连续净值曲线。
        # best-effort: 表未建 (GO 前) / 写失败只 warning, 内存态与扫描主流程不受影响。
        try:
            conn = self.db.get_connection()
            conn.execute(
                "INSERT INTO intraday_pnl_snapshots "
                "(snapshot_time, unrealized_pnl_pct, active_count) VALUES (?, ?, ?)",
                (snap["time"], snap["unrealized_pnl_pct"], snap["active_count"]))
            conn.commit()
        except Exception as e:
            logger.warning(f"intraday_pnl_snapshots 写入失败 (non-blocking): {e}")

    def get_intraday_pnl_trend(self) -> list[dict]:
        """D5 通电: 优先读库 (当日全量快照, 跨进程连续), 库不可用回退内存态。"""
        try:
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT snapshot_time, unrealized_pnl_pct, active_count "
                "FROM intraday_pnl_snapshots WHERE snapshot_time LIKE ? "
                "ORDER BY snapshot_time", (bj_now()[:10] + "%",),
            ).fetchall()
            if rows:
                return [{"time": r["snapshot_time"],
                         "unrealized_pnl_pct": r["unrealized_pnl_pct"],
                         "active_count": r["active_count"]} for r in rows]
        except Exception:
            pass
        return list(self._pnl_snapshots)

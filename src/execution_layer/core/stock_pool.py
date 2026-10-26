"""股票池 — 5 状态状态机"""

from dataclasses import dataclass, field

from common.database import Database
from common.logging import get_logger
from execution_layer.core.action_record import ActionRecord

logger = get_logger("execution.pool")

# ---- AUD-027: 了结事件字段 (单一定义点) ------------------------------------
# 描述**上一次了结事件**的字段。行重新「活着」时它们全是上一世的残留。
# 生产实证 (2026-09-14): 600152 被打回 watch 时一个没清 -> ①按状态圈的回填
# 作用域变盲 ②二次退出覆盖首次了结日 (300906: stock_pool 说 09-14、
# post_exit_tracking 说 09-07, 同一笔了结两张表两个故事)。
# 新增了结字段务必登记到此处 —— 漏登记 = 下次照样脏 (同 AUD-008 白名单族病)。
EXIT_EVENT_FIELDS = (
    "exit_reason", "exit_reason_detail", "exit_layer", "exit_date", "exit_source",
    "exit_price", "exit_low", "exit_high", "exit_plan_date",
    "trigger_price", "trigger_date",
    "total_return", "total_return_pct", "net_return", "net_return_pct",
)
# 计数/费用 (partial_exit_count / swap_count / add_position_count / total_fees)
# 是**累计历史**, 复活时不清 —— 那是 AUD-007 建立的语义, 不自拆。

# 行「活着」的状态: 这三态下任何了结字段都是残留
ALIVE_STATUSES = frozenset({"watch", "pending_entry", "active"})

# ---- D席 fix/pool-position-guard: 候选态守卫 (单一定义点) -------------------
# 病理 (2026-09-16 定谳, 统筹 0270ea38 派单 + 审计官证据链): 策略轮 add_to_watch
# 不检查目标是否已持仓, 把「已持仓待结算」的股当新候选重新入池 → update_status
# 覆写 → 了结字段被清空 + 状态机矛盾。实测 688029 (watch/600股) 与
# 003004 (pending_entry/600股); 09-19 expire_stale_watch 命中 688029
# → 600 股以 watch_expired 出场, exit_price=NULL → 真实成交损益永久丢失。
# 修法: 唯一写入入口 (update_status) 校验「候选态不得带仓」—— 抽公共守卫,
# 一处覆盖 14 处调用方 + expire_stale_pending(经正门) + 根因路径。
# 留痕: 走 ops 域 AlertManager (铁律#12 降级不得静默), 不走 action_records
#       (避 AUD-028 的 CHECK 面 DDL 变更)。
CANDIDATE_STATUSES = frozenset({"watch", "pending_entry"})


def exit_fields_null_sql(exclude: tuple = ()) -> str:
    """生成 `col=NULL, ...` 片段。exclude = 调用方自己正在写的列。

    四个写点共用这一个生成器, 不抄四遍 —— AUD-026 的教训是「绕行路径比正门多」,
    抄四遍就等于留四个各自漂移的口径。
    列名全部来自上面的模块常量, 无外部输入, 无注入面。
    """
    return ", ".join(f"{c}=NULL" for c in EXIT_EVENT_FIELDS if c not in exclude)



@dataclass
class RiskStats:
    worst: float = 0.0
    avg_loss: float = 0.0


@dataclass
class StateMachineInfo:
    current: str = ""
    transitions: list = field(default_factory=list)


class StockPool:
    def __init__(self, db: Database, config=None):
        self.db = db
        self.config = config
        cfg_get = (config.get if hasattr(config, 'get') else lambda k, d=None: d)
        self.max_positions = cfg_get("execution.max_positions", 10)
        self.max_single_stock_pct = cfg_get("execution.max_single_stock_pct", 20.0)
        self.action_record = ActionRecord(db)

    def _ensure_table(self):
        from execution_layer.tables import STOCK_POOL_DDL
        conn = self.db.get_connection()
        conn.execute(STOCK_POOL_DDL)
        conn.commit()

    def add(self, code: str, name: str, source: str = "strategy", reason: str = "",
            score: float = 0.0):
        self._ensure_table()
        conn = self.db.get_connection()
        from common.utils import bj_date
        today = bj_date()
        conn.execute("""
            INSERT OR IGNORE INTO stock_pool
            (code, name, status, source, added_date, initial_score, added_reason)
            VALUES (?, ?, 'watch', ?, ?, ?, ?)
        """, (code, name, source, today, score, reason))
        conn.commit()
        self.action_record.write(code, "add_to_watch", f"添加 {name} 到股票池 (来源:{source})", source=source)

    def get_by_status(self, *statuses: str) -> list[dict]:
        self._ensure_table()
        conn = self.db.get_connection()
        placeholders = ", ".join(["?" for _ in statuses])
        rows = conn.execute(
            f"SELECT * FROM stock_pool WHERE status IN ({placeholders}) ORDER BY added_date DESC",
            statuses
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, code: str) -> dict | None:
        self._ensure_table()
        conn = self.db.get_connection()
        row = conn.execute("SELECT * FROM stock_pool WHERE code = ?", (code,)).fetchone()
        return dict(row) if row else None

    def update(self, code: str, **kwargs):
        self._ensure_table()
        conn = self.db.get_connection()
        valid_columns = {"entry_price", "stop_loss", "take_profit", "trailing_stop", "atr_stop", "entry_low", "entry_high",
                         "dynamic_stop_loss", "dynamic_stop_reason", "last_price", "last_price_date",
                         "unrealized_pnl", "unrealized_pnl_pct", "holding_days", "position_qty", "entry_date", "exit_price", "exit_date",
                         "name", "reason", "source", "highest_price", "total_return", "total_return_pct",
                         "current_score", "position_pct", "total_fees", "net_return", "net_return_pct",
                         "holding_days_actual", "avg_entry_price",
                         "valid_until", "valid_from",  # ST-36: 复评续持延长持有期
                         "rr_ratio"}  # REC 模块2 (2026-09-21): 风险收益比
        safe_kwargs = {k: v for k, v in kwargs.items() if k in valid_columns}
        # AUD-008: 白名单丢弃必须显形 —— 拼错列名/新增列未登记时, 旧实现静默丢弃,
        # 调用方拿到 None 无法区分"已写入"与"被丢掉" (静默跳过家族)。
        dropped = [k for k in kwargs if k not in valid_columns]
        if dropped:
            logger.warning(
                f"StockPool.update({code}): {len(dropped)} 个列名不在白名单被丢弃 "
                f"-> {sorted(dropped)} (白名单见 valid_columns; 新增列须同步登记)")
        if not safe_kwargs:
            return
        sets = ", ".join(f"{k}=?" for k in safe_kwargs)
        conn.execute(f"UPDATE stock_pool SET {sets} WHERE code=?", (*safe_kwargs.values(), code))
        conn.commit()

    def _guard_candidate_status_no_position(self, conn, code: str,
                                            new_status: str) -> bool:
        """候选态守卫: 带仓 (position_qty>0) 的行不得被打回候选态。

        返回 True = 放行; False = 已拒写 (并已留痕告警)。

        判据 (统筹裁定 ③): new_status ∈ {watch, pending_entry} 且该行
        position_qty>0 → 拒。实测边界吻合: 09-18 那 5 只到期票 qty=0 不受影响;
        exit_pending/active/exited 目标态不受影响 (688332 对照组安全)。
        """
        if new_status not in CANDIDATE_STATUSES:
            return True
        row = conn.execute(
            "SELECT status, position_qty FROM stock_pool WHERE code=?",
            (code,)).fetchone()
        if not row:
            return True
        cur_status = row["status"]
        qty = row["position_qty"] or 0
        if qty <= 0:
            return True
        # 拒写 + 显形 (铁律#12: 降级不得静默)
        logger.warning(
            f"StockPool.update_status({code} → {new_status}) 拒绝: 带仓 {qty} 股 "
            f"(当前 {cur_status}) 不得进候选态 (pool-position-guard)")
        try:
            from ops_layer.alert_manager import AlertManager
            AlertManager(self.db).create_alert(
                title="持仓票被拒改候选态 (pool-position-guard)",
                detail=(f"code={code} 当前态={cur_status} position_qty={qty} "
                        f"被拒目标态={new_status}"),
                severity="warning", layer="execution")
        except Exception as e:
            # 告警链路故障不得炸主链 (拒写已生效, 只记日志)
            logger.warning(f"pool-position-guard 告警失败 (拒写已生效): {e}")
        return False

    def update_status(self, code: str, new_status: str):
        self._ensure_table()
        conn = self.db.get_connection()
        if not self._guard_candidate_status_no_position(conn, code, new_status):
            return
        detail = ""
        if new_status in ALIVE_STATUSES:
            # AUD-027: 行重新活过来 -> 上一世的了结事件字段必须清。
            # 先读出真被清掉的是哪几个 —— 清字段是**有损动作**, 不留痕就只是
            # 换个姿势的静默覆盖 (本次审计一路在治的就是静默家族)。
            row = conn.execute(
                f"SELECT {', '.join(EXIT_EVENT_FIELDS)} FROM stock_pool WHERE code=?",
                (code,)
            ).fetchone()
            stale = [f for f, v in zip(EXIT_EVENT_FIELDS, row) if v is not None] if row else []
            if stale:
                logger.warning(
                    f"StockPool.update_status({code} → {new_status}): 清除上一世了结字段 "
                    f"{len(stale)} 个 -> {stale} (AUD-027)")
                detail = f"cleared_exit_fields={len(stale)}:{','.join(stale)}"
            conn.execute(
                f"UPDATE stock_pool SET status=?, {exit_fields_null_sql()}, "
                "updated_at=datetime('now','+8 hours') WHERE code=?",
                (new_status, code))
        else:
            conn.execute(
                "UPDATE stock_pool SET status=?, updated_at=datetime('now','+8 hours') WHERE code=?",
                (new_status, code))
        conn.commit()
        self.action_record.write(code, "status_change", f"状态变更 → {new_status}",
                                 action_detail=detail)

    def mark_exit_trigger(self, code: str, trigger_price: float, reason: str,
                          exit_low: float | None = None) -> bool:
        """F4.4: 盘中触发标记 — active → exit_pending + trigger_price/date。

        只标记不结算; 结算唯一入口 = ExecutionLayer._settle_pending_exits (T+1 收盘)。
        幂等: 已是 exit_pending/exited 时跳过, 返回 False。
        """
        self._ensure_table()
        conn = self.db.get_connection()
        row = conn.execute("SELECT status FROM stock_pool WHERE code=?", (code,)).fetchone()
        if not row or row["status"] != "active":
            return False
        from common.utils import bj_date
        conn.execute("""
            UPDATE stock_pool SET
                status='exit_pending', trigger_price=?, trigger_date=?,
                exit_reason=?, exit_low=?,
                updated_at=datetime('now','+8 hours')
            WHERE code=? AND status='active'
        """, (trigger_price, bj_date(), reason, exit_low, code))
        conn.commit()
        self.action_record.write(
            code, "exit_check", f"盘中触发: {reason} (T+1延迟结算)", "execution",
            price=trigger_price,
        )
        logger.info(f"[{code}] 退出触发标记: {reason} @ {trigger_price} → exit_pending (T+1 结算)")
        return True

    def get_active(self) -> list[str]:
        items = self.get_by_status("active")
        return [i["code"] for i in items]

    def get_pending(self) -> list[str]:
        items = self.get_by_status("pending_entry")
        return [i["code"] for i in items]

    def get_exit_pending(self) -> list[str]:
        items = self.get_by_status("exit_pending")
        return [i["code"] for i in items]

    def get_watch(self) -> list[str]:
        items = self.get_by_status("watch")
        return [i["code"] for i in items]

    def get_all(self) -> list[dict]:
        return self.get_by_status("watch", "pending_entry", "active", "exit_pending", "exited")

    def count_by_status(self) -> dict[str, int]:
        self._ensure_table()
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM stock_pool GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    # ---- 调仓族 (AUD-007/010 甲案: 内脏自洽, 候接线) --------------------
    # 五个面早已在册 (四写方法 + 四计数列 + avg_entry_price + action_records
    # 词表 + HITL 中文标签), 但零生产调用方。甲案只修内脏不接线 —— 让它从
    # "零面通电"变"内部自洽候接线", 将来接调用方不必回头补内脏。
    # 整件通电 (接投顾/HITL 发加减仓建议) 属功能开发, 另立卡走设计评审。

    def _require_active(self, current: dict | None, code: str, op: str) -> bool:
        """AUD-007: 非 active 时**显形**。

        旧实现四者都是 `if current and status=='active': 做事` **无 else** ——
        非 active 静默返回 None, 调用方分不清"做了"与"被丢了"(静默跳过家族)。
        """
        if current and current.get("status") == "active":
            return True
        got = (current or {}).get("status") if current else "不存在"
        logger.warning(
            f"StockPool.{op}({code}) 跳过: 仅 active 持仓可调仓, 当前 status={got}")
        return False

    def add_position(self, code: str, shares: int, price: float, reason: str) -> bool:
        from execution_layer.core.constraint_checker import calculate_fees
        conn = self.db.get_connection()
        current = self.get(code)
        if not self._require_active(current, code, "add_position"):
            return False
        fees = calculate_fees(price, shares, is_buy=True)
        old_qty = current.get("position_qty") or 0
        # AUD-010: avg_entry_price 有 DDL+migration+白名单却**零写者** ——
        # 旧实现加仓只加量不改均价, 于是加仓后的盈亏拿**第一次**入场价算。
        # 首次加仓时该列尚为 NULL, 以 entry_price 为起点。
        old_avg = current.get("avg_entry_price") or current.get("entry_price") or 0.0
        new_qty = old_qty + shares
        new_avg = ((old_qty * old_avg + shares * price) / new_qty) if new_qty > 0 else old_avg
        conn.execute(
            "UPDATE stock_pool SET position_qty = ?, avg_entry_price = ?, "
            "total_fees = COALESCE(total_fees, 0) + ?, "
            "add_position_count = COALESCE(add_position_count, 0) + 1, "
            "updated_at = datetime('now','+8 hours') WHERE code = ?",
            (new_qty, round(new_avg, 4), fees["total"], code))
        conn.commit()
        self.action_record.write(
            code, "add_position", reason, "execution",
            price=price, new_value=fees["total"],
            action_detail=f"shares={shares},price={price},avg={round(new_avg, 4)}")
        return True

    def reduce_position(self, code: str, shares: int, price: float, reason: str) -> bool:
        conn = self.db.get_connection()
        current = self.get(code)
        if not self._require_active(current, code, "reduce_position"):
            return False
        # 减仓只减量不改均价 (会计常识: 卖出不影响持仓成本)
        new_qty = max(0, (current.get("position_qty") or 0) - shares)
        conn.execute(
            "UPDATE stock_pool SET position_qty = ?, "
            "reduce_position_count = COALESCE(reduce_position_count, 0) + 1, "
            "updated_at = datetime('now','+8 hours') WHERE code = ?",
            (new_qty, code))
        conn.commit()
        self.action_record.write(code, "reduce_position", reason, "execution", price=price)
        return True

    def partial_exit(self, code: str, pct: float, price: float, reason: str) -> bool:
        from common.utils import bj_date
        from execution_layer.core.constraint_checker import calculate_fees
        conn = self.db.get_connection()
        current = self.get(code)
        if not self._require_active(current, code, "partial_exit"):
            return False
        current_qty = current.get("position_qty") or 0
        sell_qty = int(current_qty * pct / 100)
        new_qty = max(0, current_qty - sell_qty)
        fees = calculate_fees(price, sell_qty, is_buy=False)
        self.action_record.write(
            code, "partial_exit", f"{reason} (卖出{pct:.0f}%)", "execution",
            price=price, new_value=fees["total"],
            action_detail=f"shares={sell_qty},price={price}")
        if new_qty == 0:
            conn.execute(
                "UPDATE stock_pool SET status='exited', position_qty=0, exit_price=?, "
                "exit_date=?, total_fees = COALESCE(total_fees, 0) + ?, "
                "partial_exit_count = COALESCE(partial_exit_count, 0) + 1, "
                "updated_at = datetime('now','+8 hours') WHERE code=?",
                (price, bj_date(), fees["total"], code))
        else:
            conn.execute(
                "UPDATE stock_pool SET position_qty = ?, "
                "total_fees = COALESCE(total_fees, 0) + ?, "
                "partial_exit_count = COALESCE(partial_exit_count, 0) + 1, "
                "updated_at = datetime('now','+8 hours') WHERE code = ?",
                (new_qty, fees["total"], code))
        conn.commit()
        return True

    def swap(self, old_code: str, new_code: str, reason: str) -> bool:
        from common.utils import bj_date

        conn = self.db.get_connection()
        old = self.get(old_code)
        if not self._require_active(old, old_code, "swap"):
            return False
        # AUD-005 同判别标准: exit_date 写**日期**不写时间戳 —— 旧实现写
        # datetime(...) 全时间戳, 而 execution_stats.save_daily 用 `exit_date=?`
        # 精确等值比当日日期串, 时间戳形态永远匹配不上 -> 这些了结静默不进当日统计。
        with conn:
            cursor = conn.execute(
                "UPDATE stock_pool SET status='exited', exit_date=?, "
                "swap_count = COALESCE(swap_count, 0) + 1, "
                "updated_at = datetime('now','+8 hours') WHERE code=? AND status='active'",
                (bj_date(), old_code))
            if cursor.rowcount != 1:
                return False
            self.action_record.write(
                old_code, "swap_exit", f"调仓换股 → {new_code}: {reason}", "execution",
                connection=conn)
        return True


    def get_weighted_avg_price(self, code: str) -> float:
        """Calculate weighted average entry price from stock_pool"""
        pool = self.get(code)
        if not pool:
            return 0.0
        # AUD-010 第三面: 本方法名为"加权均价"但旧实现直接 return entry_price ——
        # 根本没算任何平均。avg_entry_price 列现由 add_position 维护(甲案),
        # 有值即用真均价, 无值(未加过仓)回落 entry_price —— 语义等价且诚实。
        entry_price = pool.get("entry_price") or 0
        position_qty = pool.get("position_qty") or 0
        avg = pool.get("avg_entry_price") or entry_price
        if position_qty > 0 and avg > 0:
            return round(avg, 2)
        return 0.0

    def get_risk_stats(self) -> RiskStats:
        """返回已退出股票的风险统计"""
        if not self.db:
            return RiskStats()
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT MIN(total_return_pct) as worst, "
            "AVG(CASE WHEN total_return_pct < 0 THEN total_return_pct ELSE NULL END) as avg_loss "
            "FROM stock_pool WHERE status='exited'"
        ).fetchone()
        return RiskStats(worst=row["worst"] or 0, avg_loss=row["avg_loss"] or 0)

    def filter_by_criteria(self, status: str = "", min_return: float = -999,
                           max_return: float = 999, date_from: str = "",
                           date_to: str = "", limit: int = 1000) -> list[dict]:
        """按条件过滤股票池记录"""
        if not self.db:
            return []
        conn = self.db.get_connection()
        conditions = []
        params = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if min_return > -999:
            conditions.append("COALESCE(total_return_pct, -999) >= ?")
            params.append(min_return)
        if max_return < 999:
            conditions.append("COALESCE(total_return_pct, 999) <= ?")
            params.append(max_return)
        if date_from:
            conditions.append("added_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("added_date <= ?")
            params.append(date_to)
        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM stock_pool WHERE {where} ORDER BY added_date DESC LIMIT ?",
            params
        ).fetchall()
        return [dict(r) for r in rows]

    def get_order_state_machine(self, code: str) -> StateMachineInfo:
        pool = self.get(code)
        if not pool:
            return StateMachineInfo(current="nonexistent", transitions=["add_to_watch"])
        status = pool["status"]
        transitions = {
            "watch": ["pending_entry", "exited"],
            "pending_entry": ["active", "watch"],
            "active": ["exit_pending", "exited"],
            "exit_pending": ["active", "exited"],
            "exited": [],
        }
        return StateMachineInfo(current=status, transitions=transitions.get(status, []))

    def get_available_cash(self, initial_capital: float = 300000.0) -> float:
        """动态计算可用现金 = 初始资金 - 在投成本 - 已退出入场成本 + 已退出回款

        D4: 原公式漏扣 exited 股票的入场成本 — 回款加回却不扣成本, 每笔已退出
        交易的成本被重复计入现金 (生产实测虚增 196,033)。资金守恒:
        初始 = 现金 + 在投成本 + Σexited(入场成本 − 回款)。
        """
        conn = self.db.get_connection()
        row = conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN status IN ('active', 'exit_pending') THEN entry_price * position_qty ELSE 0 END), 0) as invested,
                COALESCE(SUM(CASE WHEN status='exited' THEN entry_price * position_qty ELSE 0 END), 0) as exited_cost,
                COALESCE(SUM(CASE WHEN status='exited' THEN exit_price * position_qty ELSE 0 END), 0) as proceeds
            FROM stock_pool
        """).fetchone()
        invested = row["invested"] or 0
        exited_cost = row["exited_cost"] or 0
        proceeds = row["proceeds"] or 0
        cash = initial_capital - invested - exited_cost + proceeds
        # 包D (Day 4 统 筹批): clamp cash >= 0 必 闸,
        # 负 值 log.error 判 例 #137 + 落 alert 给 守 0 不 负
        # (现金是 负 的 = 账 目 不变 量 破坏, silent return negative = 造 假 数据 同族)
        if cash < 0:
            logger.error(
                f"[判例#137] get_available_cash 现 负 现金 cash={cash:.2f} "
                f"(initial={initial_capital:.2f}, invested={invested:.2f}, "
                f"exited_cost={exited_cost:.2f}, proceeds={proceeds:.2f}) — "
                f"现金 是 负 的 是 账 目 不变 量 破坏,守 卫给 0 不 负")
            try:
                self._alert_negative_cash(cash, initial_capital,
                                          invested, exited_cost, proceeds)
            except Exception as _e:
                logger.warning(f"negative cash alert failed (fail-safe): {_e}")
            return max(cash, 0.0)
        return cash

    def _alert_negative_cash(self, cash: float, initial_capital: float,
                              invested: float, exited_cost: float,
                              proceeds: float):
        """落 alert 到 alerts 表 (判例 #9 告警 必 有 载 体)。fail-safe: 落 库 失 仅 warning。"""
        from datetime import datetime
        conn = self.db.get_connection()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO alerts "
            "(severity, layer, title, detail, action, status, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
            ("critical", "execution",
             f"negative cash invariant broken: {cash:.2f}",
             f"invested={invested:.2f}, exited_cost={exited_cost:.2f}, "
             f"proceeds={proceeds:.2f}, initial_capital={initial_capital:.2f}",
             "审 stock_pool action_records 与 NAV 账 目 不变 量",
             now, now))
        conn.commit()

    def expire_stale_pending(self, today: str | None = None) -> list[str]:
        """S5: 入场区间过期的 pending_entry 清理 — 作废回观察池 (不再每轮重评)

        生产实害 (08-31): 4 笔 08-28 深夜挂入的 pending valid_until=08-28 (写侧
        日历 bug 已另修), 过期后每 5 分钟一轮触价检查都在 check_entry 过期闸门
        被拒, 每轮刷一条 entry_blocks 行 (~200 行/日), 002508 带内触价 46 轮被拦。

        迁移目标 = watch + cancel_entry 留痕 — S4 holding.py 先例 (CHECK 白名单
        五值内, 语义诚实「入场计划作废回观察池」, 不伪装 exited, 不新造状态值)。
        valid_until 为空不迁移 (存量 S5 前语义, 宁可不误伤)。
        """
        from common.utils import bj_date
        conn = self.db.get_connection()
        as_of = today if today is not None else bj_date()
        rows = conn.execute(
            "SELECT code, name, valid_until, position_qty FROM stock_pool "
            "WHERE status='pending_entry' AND valid_until IS NOT NULL "
            "AND valid_until < ?",
            (as_of,)
        ).fetchall()
        # 守卫腿二: 带仓票不许被推回 watch。见 expire_stale_watch 的前提说明。
        # 003004 生产实例: pending_entry 带 600 股, valid_until 到期后会被推成 watch,
        # 随即落进腿一的 added_date 判据 —— 但**防御不能建立在两步链上**,
        # 第一步若改了行为, 腿一的守卫就落空。这条腿自己挡自己的。
        held = [r for r in rows if (r["position_qty"] or 0) > 0]
        rows = [r for r in rows if (r["position_qty"] or 0) <= 0]
        if held:
            self._alert_held_in_expiry(held, "expire_stale_pending")
        expired = []
        for r in rows:
            self.update_status(r["code"], "watch")
            self.action_record.write(
                r["code"], "cancel_entry",
                f"入场区间过期 (valid_until={r['valid_until']}), 作废回观察池"
            )
            expired.append(r["code"])
            logger.info(
                f"Pending expired: {r['code']} {r['name']} "
                f"valid_until={r['valid_until']}"
            )
        return expired

    def _alert_held_in_expiry(self, held: list, leg: str) -> None:
        """守卫拦下带仓票时告警 —— **拦下来不出声 = 换个地方再犯同一个病**。

        静默地"正确处理"一个本不该发生的状态, 等于让根因继续潜伏。
        688029 在被拦之前已经 `skip_entry` 刷了 67 条/日 —— 系统喊了一整天,
        缺的是把「买不进」和「已持有 600 股」连起来的那一跳。守卫若也不出声,
        它就永远停在"被拦着但没人修"。

        title 跨 N 稳定: AlertManager 靠同名 open 抑制去重 (alert_manager.py:41),
        title 里塞数量/代码 → 每天一条新 open → 积压天天涨 (AUD-019 成因)。
        fail-safe 但不静默: 告警炸了只 warning, **绝不连累守卫本身** ——
        二者耦合的话, 告警一坏带仓票就又会被清, 那是最坏的失败方向。
        """
        try:
            from ops_layer.alert_manager import AlertManager
            from ops_layer.core.models import Alert

            items = ", ".join(
                f"{r['code']}({int(r['position_qty'] or 0)}股)" for r in held)
            AlertManager(self.db).send(Alert(
                severity="critical",
                layer="execution",
                title="持仓票误入到期清理",
                detail=(f"{len(held)} 只**带持仓**的票被到期清理扫中, 已拦下未清: "
                        f"{items}。腿={leg}。"
                        f"根因在写入方 —— 策略轮 add_to_watch 未校验目标是否已持仓, "
                        f"把已持仓待结算的票当新候选重新入池 (688029/003004 实证)。"
                        f"本守卫只止血, 状态仍是错的, 需按根因修。"),
                action="拦下未清, 需人工复位状态并修根因",
            ))
        except Exception as e:  # noqa: BLE001 — 告警失败不许连累守卫
            logger.warning(f"持仓票到期守卫: 告警发送失败 ({leg}): {e}")

    def expire_stale_watch(self, expire_days: int = 7) -> list[str]:
        """清理过期的 watch 状态股票（超过 expire_days 天未升级）。

        **前提: watch 态的票没有持仓。** AUD-027 清了结字段正是建立在这个前提上 ——
        一只从未被买入的票不该有卖出价/收益 (300906 幽灵交易就是这么进统计面的)。

        该前提对**真正的** watch 票成立, 但它此前只活在我脑子里、没写进代码,
        于是成了**隐含假设**; 而隐含假设会在下一个风险面上失效:
        2026-09-16 取证, 688029 带 600 股落在 watch 态 (策略轮 add_to_watch 覆写了
        exit_pending), 按 expire_days=7 将于 **09-19** 被本函数清成
        `exit_reason='watch_expired'`、`exit_price=NULL`、`total_return=NULL` ——
        **一笔真实成交的损益永久丢失**。且因 AUD-027 已把残留字段清干净,
        这笔损失会以"干净"的形态发生, **比旧实现更不容易被看见**。

        → 判例: **修复的前提若未写进注释, 它就不是前提, 是隐含假设。**
        → 故加下方守卫: position_qty>0 的行**拒绝到期**并告警, 不静默清出。
        """
        from datetime import datetime, timedelta

        from common.utils import bj_date
        conn = self.db.get_connection()
        cutoff = (datetime.strptime(bj_date(), "%Y-%m-%d") - timedelta(days=expire_days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT code, name, position_qty FROM stock_pool "
            "WHERE status='watch' AND added_date < ?",
            (cutoff,)
        ).fetchall()
        # 守卫腿一: 带仓票拒绝到期 (见上方「前提」)。逐行判, 不是一行有仓就整批放弃。
        held = [r for r in rows if (r["position_qty"] or 0) > 0]
        rows = [r for r in rows if (r["position_qty"] or 0) <= 0]
        if held:
            self._alert_held_in_expiry(held, "expire_stale_watch")
        expired = []
        track_exit_source = False
        try:
            from config_layer import cfg_get
            track_exit_source = bool(cfg_get("execution.track_exit_source", False))
        except Exception:
            track_exit_source = False

        # AUD-027: 观察到期**不是一笔交易** —— 一只 watch 票从未被买入, 不该有
        # 卖出价/收益/触发价。旧实现只写 reason/date/source, 其余字段原样继承,
        # 于是 300906 带着 exit_price=29.43、total_return_pct=0.00% 以
        # 'watch_expired' 身份进了 execution_stats (幽灵交易)。这里显式清掉
        # 自己不写的那些列 —— exclude 里是本 SQL 自己负责的列。
        null_with_src = exit_fields_null_sql(
            exclude=("exit_reason", "exit_date", "exit_source"))
        null_no_src = exit_fields_null_sql(exclude=("exit_reason", "exit_date"))
        for r in rows:
            if track_exit_source:
                conn.execute(
                    f"UPDATE stock_pool SET status='exited', exit_reason='watch_expired', "
                    f"exit_source='cleanup', {null_with_src}, "
                    "exit_date=?, updated_at=datetime('now','+8 hours') WHERE code=?",
                    (bj_date(), r["code"])
                )
            else:
                conn.execute(
                    f"UPDATE stock_pool SET status='exited', exit_reason='watch_expired', "
                    f"{null_no_src}, "
                    "exit_date=?, updated_at=datetime('now','+8 hours') WHERE code=?",
                    (bj_date(), r["code"])
                )
            self.action_record.write(r["code"], "remove_from_watch", f"观察池过期({expire_days}天)")
            expired.append(r["code"])
            logger.info(f"Watch expired: {r['code']} {r['name']}")
        if expired:
            conn.commit()
        return expired

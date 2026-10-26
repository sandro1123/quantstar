"""策略A 持有期状态机 — 【到期待复评】派生态 (派发 2026-08-24 交付物②)

设计决定 (回执 S-1):
- stock_pool.status 有 CHECK 五值枚举, 物理加态 = 整表重建 = GO 级迁移 → 不做。
- awaiting_review 是**派生态**: 物理 status 保持不变, 本模块每次运行计算
  effective_state 并据此驱动复评。落库只走 StockPool 既有方法 (update_status /
  mark_exit_trigger / update), 策略层不直写 stock_pool。
- exit_engine L5 (30天强制退出) 不改: 复评在 5-10 交易日触发, 总是先于 L5;
  L5 保留为无限续持的兜底安全网。

状态视图 (effective_state):
  pending_entry ─(持有满 min_hold_days)→ awaiting_review [物理仍 pending_entry]
  active ───────(持有满 min_hold_days)→ awaiting_review [物理仍 active]
  awaiting_review ─(复评通过)→ 续持: valid_until 延长 + action_records 'hold'
  awaiting_review ─(复评不过):
      active        → mark_exit_trigger (active→exit_pending; 结算唯一入口 _settle_pending_exits 不变)
      pending_entry → update_status('watch') + action_records 'cancel_entry'
                      (入场计划作废回观察池; mark_exit_trigger 只认 active, 硬走会静默 False)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.calendar import TradingCalendar
from common.logging import get_logger

logger = get_logger("strategy.lifecycle.holding")

# 派生态名 — 只存在于本模块的判定结果里, 永远不写入 stock_pool.status
STATE_AWAITING_REVIEW = "awaiting_review"


@dataclass
class ReviewDecision:
    """单只股票的到期复评决策输出。"""
    code: str
    physical_status: str          # stock_pool.status 当前值
    effective_state: str          # 物理态透传 | awaiting_review | <物理态>:undated
    action: str                   # none | review | trigger_exit | flag_stale
    trading_days_held: int | None = None
    valid_until: str | None = None
    detail: dict = field(default_factory=dict)


def _calendar(db) -> TradingCalendar:
    cal = TradingCalendar(db=db)
    cal._load_from_db()
    return cal


def trading_days_between(db, start_date: str, end_date: str) -> int | None:
    """start_date 之后到 end_date 为止经历的交易日数 (start 当日不计)。

    口径与 F-37 对齐: 交易日非自然日。日历无数据返回 None (不可判定 ≠ 0)。
    """
    if not start_date or not end_date or end_date < start_date:
        return None
    cal = _calendar(db)
    days = cal.trading_days_in_range(start_date, end_date)
    if not days:
        return None
    return sum(1 for d in days if d > start_date)


def effective_state(db, row: dict, as_of: str,
                    min_hold_days: int = 5) -> tuple[str, int | None]:
    """计算单行有效状态, 返回 (effective_state, trading_days_held)。

    - pending_entry / active 且持有 >= min_hold_days → awaiting_review
    - watch / exit_pending / exited 透传
    - 到期锚点优先 entry_date, 缺失回退 added_date (DDL 里 added_date NOT NULL,
      故 :undated 仅在两列皆空时出现, 防御性保留)
    - 日历断供时 held=None 且状态透传 — 本轮跳过该股并留 detail, 不当 0 处理
    """
    status = row.get("status", "")
    if status in ("watch", "exit_pending", "exited"):
        return status, None

    anchor = row.get("entry_date") or row.get("added_date")
    if not anchor:
        return f"{status}:undated", None
    held = trading_days_between(db, anchor, as_of)
    if held is None:
        return status, None  # 日历不可判定: 显式跳过, 不伪造持有天数
    if held >= min_hold_days:
        return STATE_AWAITING_REVIEW, held
    return status, held


class HoldingStateMachine:
    """持有期状态机 — 每交易日收盘后跑一轮。

    用法 (接线点, 本派发不接 crontab):
        sm = HoldingStateMachine(db, stock_pool, heat_assessor=assess_heat)
        report = sm.run(as_of="2026-08-24")
    """

    def __init__(self, db, stock_pool=None, heat_assessor=None,
                 min_hold_days: int = 5, max_hold_days: int = 10):
        self.db = db
        self.pool = stock_pool
        self.heat_assessor = heat_assessor  # callable(db, code, as_of) -> HeatAssessment
        self.min_hold_days = min_hold_days
        self.max_hold_days = max_hold_days

    # -- 到期判定 (只读) ------------------------------------------------------

    def scan(self, as_of: str) -> list[ReviewDecision]:
        """扫全池返回逐行决策。纯只读, dry-run 安全。"""
        conn = self.db.get_connection()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM stock_pool WHERE status IN ('watch','pending_entry','active') "
            "ORDER BY added_date").fetchall()]
        decisions: list[ReviewDecision] = []
        for row in rows:
            code = row["code"]
            eff, held = effective_state(self.db, row, as_of, self.min_hold_days)
            base = {"code": code, "physical_status": row.get("status", ""),
                    "trading_days_held": held, "valid_until": row.get("valid_until")}
            if eff == STATE_AWAITING_REVIEW:
                overdue = held > self.max_hold_days
                decisions.append(ReviewDecision(
                    effective_state=eff,
                    action="trigger_exit" if overdue else "review",
                    detail={"overdue": overdue, "max_hold_days": self.max_hold_days},
                    **base))
            elif eff.endswith(":undated"):
                decisions.append(ReviewDecision(
                    effective_state=eff, action="flag_stale",
                    detail={"reason": "no_entry_date_and_no_added_date"}, **base))
            elif held is not None:
                decisions.append(ReviewDecision(
                    effective_state=eff, action="none", **base))
            # held None (日历断供): 本轮跳过, 不打扰
        return decisions

    # -- 复评执行 (写库路径, 全部经 StockPool 方法) ----------------------------

    def run_review(self, decision: ReviewDecision, as_of: str) -> dict:
        """对单个 awaiting_review 决策执行复评并留痕。

        流程: assess_heat 五路信号 → 按建议处置 → action_records 留痕。
        写库仅经 StockPool.update / update_status / mark_exit_trigger /
        action_record.write 四个既有方法。
        """
        if self.pool is None or self.heat_assessor is None:
            raise RuntimeError("run_review 需要 stock_pool 与 heat_assessor")
        code = decision.code
        assessment = self.heat_assessor(self.db, code, as_of)
        rec = assessment.recommendation
        summary = assessment.summary()
        outcome: dict = {"code": code, "recommendation": rec, "actions": []}

        # 超期未决 (held > max) 一律退出流程, 不再续持 — 复评窗口本身有上限
        overdue = bool(decision.detail.get("overdue"))

        if not overdue and rec in ("hold", "lean_hold_review", "insufficient_signal"):
            new_valid = self._extend_until(as_of)
            self.pool.update(code, valid_until=new_valid)
            neutral_tag = " (信号不足, 中性续持)" if rec == "insufficient_signal" else ""
            self.pool.action_record.write(
                code, "hold", summary + neutral_tag, source="strategy",
                new_value=decision.trading_days_held,
                action_detail=str(assessment.to_dict()))
            outcome["actions"].append("extend_hold")
            return outcome

        # 复评不过 → 按物理状态分流
        if decision.physical_status == "active":
            last_price = row_last_price(self.db, code)
            triggered = self.pool.mark_exit_trigger(
                code,
                trigger_price=float(last_price) if last_price else 0.0,
                reason=f"{summary} | 到期复评不过")
            outcome["actions"].append(
                "trigger_exit" if triggered else "trigger_exit_skipped")
        else:  # pending_entry: 入场计划作废回观察池 (mark_exit_trigger 只认 active)
            self.pool.update_status(code, "watch")
            self.pool.action_record.write(
                code, "cancel_entry", f"{summary} | 入场计划到期未触发, 作废回观察池",
                source="strategy", action_detail=str(assessment.to_dict()))
            outcome["actions"].append("cancel_entry")

        self.pool.action_record.write(
            code, "sell_signal", summary, source="strategy",
            action_detail=str(assessment.to_dict()))
        return outcome

    # -- 一体化入口 -------------------------------------------------------------

    def run(self, as_of: str, execute: bool = True) -> dict:
        """scan → 对 awaiting_review 行执行复评。execute=False 时纯扫描。"""
        decisions = self.scan(as_of)
        due = [d for d in decisions if d.effective_state == STATE_AWAITING_REVIEW]
        stale = [d for d in decisions if d.action == "flag_stale"]
        results = []
        if execute:
            for d in due:
                try:
                    results.append(self.run_review(d, as_of))
                except Exception as exc:  # noqa: BLE001 — 单股失败不拖垮全池
                    logger.error("review failed for %s: %s", d.code, exc)
                    results.append({"code": d.code, "error": str(exc)})
        return {"as_of": as_of, "scanned": len(decisions),
                "due_for_review": len(due), "stale_flagged": len(stale),
                "decisions": [vars(d) for d in decisions], "executed": results}

    # -- 内部 ---------------------------------------------------------------------

    def _extend_until(self, as_of: str) -> str:
        """as_of 之后第 max_hold_days 个交易日。日历断供时退化为自然日估算并告警
        (不静默 — F-37 的教训是自然日/交易日混用, 兜底也必须可观测)。"""
        cal = _calendar(self.db)
        forward = [d for d in cal.trading_days_in_range(as_of, "2099-12-31") if d > as_of]
        if len(forward) >= self.max_hold_days:
            return forward[self.max_hold_days - 1]
        logger.warning("trading calendar short (%d days ahead from %s), "
                       "fallback to natural-day estimate", len(forward), as_of)
        from datetime import datetime, timedelta
        dt = datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=7 * self.max_hold_days // 5)
        return dt.strftime("%Y-%m-%d")


def row_last_price(db, code: str) -> float | None:
    """最近收盘价, 作 trigger_price。无 K 线返回 None。"""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT close FROM daily_klines WHERE code=? ORDER BY date DESC LIMIT 1",
        (code,),
    ).fetchone()
    return row["close"] if row else None

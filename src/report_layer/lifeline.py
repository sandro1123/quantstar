"""R0: 生命线读模型 — 四表纯只读 join 视图, 不写生产库 (铁律#4)。

生命线 = 一条推荐计划的完整生命周期:
    推荐入池 → 观察(watch) → 待入场(pending_entry) → 持有(active)
    → 待退出(exit_pending) → 已退出(exited, 归因 retrospect)

生命线 ID = "{code}@{plan_date}"。plan_date 优先 entry_plan_date,
为空时降级 added_date (实证 2026-08-28: stock_pool.entry_plan_date/
valid_from/exit_plan_date 三列 100% 全空 —— 「建了没通电」, 写侧接线后
本模块自动切到 entry_plan_date, 无需改动)。

数据源 (全部现成表, 零新表):
    stock_pool                状态机主体 (一行 = 一条生命线)
    action_records            动作事件流 (全类型)
    intraday_checks           盘中触价审计 (仅 triggered 入事件流)
    recommendation_retrospect 推荐归因 (AG-01 口径, code+rec_date 精确匹配)
    stock_info                名称/行业补全 (R1 新增, 可选; 缺表不崩)

消费方 (R1 晨报 / R3 晚报 / R4 连续性断言) 用 build_lifelines(conn) 取全量,
用 is_open() 裁 D7 连续性窗口 (valid_until 内未退出的生命线)。

R1 (2026-08-28): Lifeline 增补 industry / source / initial_score 三字段,
并用 stock_info 补全名称 (stock_pool.name 实证为 code 回填) 与行业。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

# 状态 → 生命线阶段 (报告用中文标签)
STAGE_MAP = {
    "watch": "观察",
    "pending_entry": "待入场",
    "active": "持有",
    "exit_pending": "待退出",
    "exited": "已退出",
}

# 事件流只收 triggered 的盘中检查; no_action 是噪声 (670 行里 370 行)
_CHECK_STATUSES = ("triggered",)

# 同一秒内 check 先于 action (触价检测 → 执行入场 的因果序)
_KIND_RANK = {"check": 0, "action": 1}


def _col(row: sqlite3.Row, key: str, default=None):
    """宽容取列: 窄 schema (如 R0 测试库) 缺列时返回 default, 不抛。"""
    return row[key] if key in row.keys() else default


@dataclass(frozen=True)
class LifelineEvent:
    """生命线上的一个事件 (动作或触价)。"""

    ts: str                    # created_at / check_time (本地时区字符串)
    kind: str                  # "action" | "check"
    label: str                 # action_type 或 "触价:{check_type}"
    price: Optional[float]
    detail: str


@dataclass(frozen=True)
class Retrospect:
    """推荐归因 (AG-01 口径), 回连推荐日。"""

    prediction_id: int
    rec_date: str
    verdict: Optional[str]
    hit_5d: Optional[int]
    hit_10d: Optional[int]
    fwd_return_5d: Optional[float]
    fwd_return_10d: Optional[float]


@dataclass(frozen=True)
class Lifeline:
    """一条生命线: 推荐计划从入池到复盘的全部状态与事件。"""

    lifeline_id: str           # "{code}@{plan_date}"
    code: str
    name: str
    plan_date: str             # ID 锚点 (entry_plan_date or added_date)
    stage: str                 # 中文阶段标签 (STAGE_MAP)
    status: str                # stock_pool.status 原值
    # 区间段
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    valid_until: Optional[str] = None
    # 持有段
    entry_price: Optional[float] = None
    entry_date: Optional[str] = None
    stop_loss: Optional[float] = None
    trailing_stop: Optional[float] = None
    dynamic_stop_loss: Optional[float] = None
    atr_stop: Optional[float] = None
    take_profit: Optional[float] = None
    # 退出段
    exit_price: Optional[float] = None
    exit_date: Optional[str] = None
    net_return_pct: Optional[float] = None
    review_quality: Optional[str] = None
    # R3 退出归因段 (stock_pool 宽列, 缺则为 None)
    exit_layer: Optional[str] = None
    exit_reason: Optional[str] = None
    exit_reason_detail: Optional[str] = None
    total_return_pct: Optional[float] = None
    holding_days_actual: Optional[int] = None
    review_note: Optional[str] = None
    # R3 盯市段 (用户视角句式需要现价/浮盈)
    last_price: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None
    holding_days: Optional[int] = None
    # R1 展示补全段 (stock_info / stock_pool 宽列, 缺则为 None)
    industry: Optional[str] = None
    source: Optional[str] = None
    entry_plan_source: Optional[str] = None
    initial_score: Optional[float] = None
    added_date: Optional[str] = None
    added_reason: Optional[str] = None
    entry_reason: Optional[str] = None
    # 事件流 (按时间升序) 与归因
    events: tuple = ()
    retrospect: Optional[Retrospect] = None


def plan_anchor(row: sqlite3.Row) -> str:
    """生命线 ID 锚点: entry_plan_date 优先, 空则降级 added_date。"""
    return row["entry_plan_date"] or row["added_date"]


def is_open(lifeline: Lifeline, as_of: str) -> bool:
    """D7 连续性窗口: 未退出 且 valid_until 未过期 (无 valid_until = 长期观察)。"""
    if lifeline.status == "exited":
        return False
    if lifeline.valid_until and lifeline.valid_until < as_of:
        return False
    return True


def _load_stock_info(conn: sqlite3.Connection) -> dict[str, tuple[str, Optional[str]]]:
    """code → (name, industry); stock_info 缺表/读取失败返回空 dict (不崩)。"""
    info: dict[str, tuple[str, Optional[str]]] = {}
    try:
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_info'"
        ).fetchone()
        if not has:
            return info
        for r in conn.execute("SELECT code, name, industry FROM stock_info"):
            info[r["code"]] = (r["name"], r["industry"])
    except Exception:
        return {}
    return info


def build_lifelines(conn: sqlite3.Connection) -> list[Lifeline]:
    """四表只读 join → 生命线列表 (stock_pool 一行 = 一条生命线)。

    conn 要求只读 (生产用 open_readonly()); 本函数零写入。
    事件按 code 归属 (生产 stock_pool 每 code 一行; 同 code 多计划行共享
    事件流, 由 plan_date 区分身份)。
    """
    conn.row_factory = sqlite3.Row
    pools = conn.execute(
        "SELECT * FROM stock_pool ORDER BY code, created_at").fetchall()

    events_by_code: dict[str, list[LifelineEvent]] = {}
    for r in conn.execute(
        "SELECT code, action_type, price, reason, created_at "
        "FROM action_records ORDER BY created_at, id"
    ):
        events_by_code.setdefault(r["code"], []).append(LifelineEvent(
            ts=r["created_at"], kind="action", label=r["action_type"],
            price=r["price"], detail=r["reason"] or ""))
    for r in conn.execute(
        "SELECT code, check_type, current_price, status, detail, check_time "
        "FROM intraday_checks WHERE status IN ({}) "
        "ORDER BY check_time, id".format(
            ",".join("?" * len(_CHECK_STATUSES))),
        _CHECK_STATUSES,
    ):
        events_by_code.setdefault(r["code"], []).append(LifelineEvent(
            ts=r["check_time"], kind="check",
            label=f"触价:{r['check_type']}", price=r["current_price"],
            detail=r["detail"] or ""))
    for evs in events_by_code.values():
        evs.sort(key=lambda e: (e.ts, _KIND_RANK[e.kind]))

    retro_by_key: dict[tuple[str, str], Retrospect] = {}
    for r in conn.execute(
        "SELECT prediction_id, code, rec_date, verdict, hit_5d, hit_10d, "
        "fwd_return_5d, fwd_return_10d FROM recommendation_retrospect "
        "WHERE code IS NOT NULL AND code != '' ORDER BY prediction_id"
    ):
        # 同 (code, rec_date) 多条时取最新 prediction_id (确定性)
        retro_by_key[(r["code"], r["rec_date"])] = Retrospect(
            prediction_id=r["prediction_id"], rec_date=r["rec_date"],
            verdict=r["verdict"], hit_5d=r["hit_5d"], hit_10d=r["hit_10d"],
            fwd_return_5d=r["fwd_return_5d"],
            fwd_return_10d=r["fwd_return_10d"])

    info_by_code = _load_stock_info(conn)

    lifelines: list[Lifeline] = []
    for row in pools:
        anchor = plan_anchor(row)
        # R1: 名称/行业补全 — stock_pool.name 实证为 code 回填, 用 stock_info 还原
        raw_name = row["name"] or row["code"]
        name = raw_name
        industry: Optional[str] = None
        info = info_by_code.get(row["code"])
        if info:
            info_name, info_industry = info
            industry = info_industry
            if not row["name"] or row["name"] == row["code"]:
                name = info_name or raw_name
        lifelines.append(Lifeline(
            lifeline_id=f"{row['code']}@{anchor}",
            code=row["code"],
            name=name,
            plan_date=anchor,
            stage=STAGE_MAP.get(row["status"], row["status"] or "未知"),
            status=row["status"],
            entry_low=row["entry_low"], entry_high=row["entry_high"],
            valid_until=row["valid_until"],
            entry_price=row["entry_price"], entry_date=row["entry_date"],
            stop_loss=row["stop_loss"], trailing_stop=row["trailing_stop"],
            dynamic_stop_loss=row["dynamic_stop_loss"],
            atr_stop=row["atr_stop"], take_profit=row["take_profit"],
            exit_price=row["exit_price"], exit_date=row["exit_date"],
            net_return_pct=row["net_return_pct"],
            review_quality=row["review_quality"],
            exit_layer=_col(row, "exit_layer"),
            exit_reason=_col(row, "exit_reason"),
            exit_reason_detail=_col(row, "exit_reason_detail"),
            total_return_pct=_col(row, "total_return_pct"),
            holding_days_actual=_col(row, "holding_days_actual"),
            review_note=_col(row, "review_note"),
            last_price=_col(row, "last_price"),
            unrealized_pnl_pct=_col(row, "unrealized_pnl_pct"),
            holding_days=_col(row, "holding_days"),
            industry=industry,
            source=_col(row, "source"),
            entry_plan_source=_col(row, "entry_plan_source"),
            initial_score=_col(row, "initial_score"),
            added_date=_col(row, "added_date"),
            added_reason=_col(row, "added_reason"),
            entry_reason=_col(row, "entry_reason"),
            events=tuple(events_by_code.get(row["code"], ())),
            retrospect=retro_by_key.get((row["code"], anchor)),
        ))
    return lifelines


def open_readonly(db_path: str = os.path.expanduser("~/qs/data/qs.db")) -> sqlite3.Connection:
    """以只读 URI 打开生产库 (报告呈现层永不写库, 铁律#4)。"""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

# -*- coding: utf-8 -*-
"""consume_approved 三闸 (估值断供卡衍生, 2026-09-25)。

事故背景 (生产实证): 审批路径的票从进队列到入池**全程不过 rr 闸** ——
`consume_approved` 的 buy 分支只有 EX-01「四参数 >0」闸。模块2 的 rr 守卫
只活在 `receive_plan` 的「高信心直接放行」那一支; 审批支零覆盖, 而审批支
才是量大的那支 (09-21 一天 25 只)。判例「半截防线=没有防线」。

叠加 daily_update 连续 4 个交易日锁饿死 (09-21~09-24), 35 单批准积压未消费;
实测 09-21 那 25 单里 68% 已漂出入场区间, 000875 现价距止损仅 0.22% ——
**直接消费 = 开仓即触止损**。故三闸:

  闸A 时效   : age > execution.approval_max_age_days (缺省 2 交易日) → 拒
  闸B 价格现实: 现价跌破 entry_low / 涨出 entry_high / 已破止损 → 拒
  闸C rr     : 风险收益比 < execution.min_rr_ratio → 拒 (补齐审批支覆盖)

运行序 = A → B → C (与统筹的"重要性优先级 rr→B→A"相反, 理由见实现注释:
陈旧单的 entry 参数本身已失效, 用失效参数算 rr 再报「rr 不合格」是误导)。
"""
import pytest

from common.database import Database
from execution_layer.core.action_record import ActionRecord
from execution_layer.core.human_in_loop import HumanInLoop
from execution_layer.core.plan_manager import PlanManager
from execution_layer.core.stock_pool import StockPool

TODAY = "2026-09-25"
CODE = "000001"


@pytest.fixture(autouse=True)
def _pin_auto_approve_off(monkeypatch):
    monkeypatch.setattr(PlanManager, "_auto_approve_enabled", lambda self: False)


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    """锁死"今天", 免测试随真实日期漂移 (判例 z80: 硬日期自然过期)。"""
    monkeypatch.setattr("execution_layer.core.plan_manager.bj_date", lambda: TODAY)


@pytest.fixture
def env():
    db = Database(":memory:")
    pool = StockPool(db)
    pool._ensure_table()
    ar = ActionRecord(db)
    ar._ensure_table()
    pm = PlanManager(db, pool, ar)
    conn = db.get_connection()
    # 交易日历来源 = daily_klines DISTINCT date (TradingCalendar._load_from_db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, open REAL, "
        "high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY(code, date))")
    for d in ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"):
        conn.execute("INSERT OR REPLACE INTO daily_klines "
                     "(code, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                     (CODE, d, 10.0, 11.0, 9.5, 10.5, 1000))
    conn.commit()
    return db, pool, pm


def _set_close(db, close, date=TODAY):
    db.get_connection().execute(
        "UPDATE daily_klines SET close=? WHERE code=? AND date=?", (close, CODE, date))
    db.get_connection().commit()


def _approve(pm, created_date=TODAY, entry_low=10.0, entry_high=11.0,
             stop_loss=9.0, take_profit=13.0, code=CODE):
    """造一条 approved 待消费单, created_at 可回拨到指定日。"""
    h = pm.human_in_loop
    h.evaluate(code, "buy", "测试单", 0.8, 0.6, entry_low=entry_low,
               entry_high=entry_high, stop_loss=stop_loss, take_profit=take_profit)
    rid = h.get_pending()[0]["id"]
    assert h.approve(rid) is True
    pm.db.get_connection().execute(
        "UPDATE human_review_queue SET created_at=? WHERE id=?",
        (f"{created_date} 16:40:00", rid))
    pm.db.get_connection().commit()
    return rid


def _skip_reasons(db, code=CODE):
    return [r[0] for r in db.get_connection().execute(
        "SELECT reason FROM action_records WHERE code=? AND action_type='skip_entry'",
        (code,))]


# ---------- 1. rr 计算单一定义点 ----------

def test_rr_ratio_is_single_definition_point():
    """rr 公式必须抽成 PlanManager 方法 — receive_plan 与 consume_approved 共用。

    判例「重复定义点 banned」: 两处各写一遍, 改一处忘另一处即防线分裂
    (688187 字段分裂同族)。
    """
    assert hasattr(PlanManager, "_rr_ratio"), (
        "缺 PlanManager._rr_ratio — rr 公式必须单一定义点, 不得在两处内联"
    )
    db = Database(":memory:")
    pm = PlanManager(db, StockPool(db), ActionRecord(db))
    # (13 − 10.5) / (10.5 − 9) = 1.667
    assert pm._rr_ratio(10.0, 11.0, 9.0, 13.0) == pytest.approx(1.6667, abs=1e-3)
    # 止损高于买价 → 分母非正 → 0.0 (不得抛异常)
    assert pm._rr_ratio(10.0, 11.0, 12.0, 13.0) == 0.0


# ---------- 2. 闸C: rr 不合格拒入池 ----------

def test_gate_rr_rejects_bad_ratio(env):
    """审批支补上 rr 闸 — 这是本卡存在的理由 (此前审批支零覆盖)。"""
    db, pool, pm = env
    # rr = (10.8 − 10.5) / (10.5 − 9) = 0.2 < 1.5
    _approve(pm, take_profit=10.8)
    _set_close(db, 10.5)

    pm.consume_approved()

    assert pool.get(CODE) is None, "rr 不合格的单不得入池"
    reasons = _skip_reasons(db)
    assert any("风险收益比" in r for r in reasons), (
        f"rr 拒绝必须留痕, 实际 skip_entry 留痕: {reasons}"
    )


# ---------- 3. 回归保护: 三闸全过正常入池 ----------

def test_healthy_order_still_enters_pool(env):
    """新鲜 + 价格在区间 + rr 合格 → 照常入池 (不得误杀)。"""
    db, pool, pm = env
    _approve(pm)
    _set_close(db, 10.5)

    n = pm.consume_approved()

    assert n == 1
    row = pool.get(CODE)
    assert row is not None and row["status"] == "pending_entry"


# ---------- 4. 闸B: 现价已破止损 ----------

def test_gate_price_rejects_below_stop_loss(env):
    """现价已跌破止损 → 拒。生产实证 000875 距止损 0.22%, 消费=开仓即触止损。"""
    db, pool, pm = env
    _approve(pm)
    _set_close(db, 8.9)  # < stop_loss 9.0

    pm.consume_approved()

    assert pool.get(CODE) is None, "已破止损的单不得入池"
    assert any("止损" in r for r in _skip_reasons(db)), "破止损拒绝必须留痕"


# ---------- 5. 闸B: 现价已涨出区间 ----------

def test_gate_price_rejects_above_entry_high(env):
    """现价已涨出买入区间 → 拒 (追高不是原推荐意图)。"""
    db, pool, pm = env
    _approve(pm)
    _set_close(db, 12.5)  # > entry_high 11.0

    pm.consume_approved()

    assert pool.get(CODE) is None, "已涨出区间的单不得入池"
    assert any("区间" in r for r in _skip_reasons(db)), "漂出区间拒绝必须留痕"


# ---------- 6. 闸B 无价可判: 放行但显式留痕, 不静默 ----------

def test_gate_price_unavailable_passes_with_explicit_trace(env, caplog):
    """取不到现价 → 不拦截(否则数据断供时又造一片积压), 但必须显式留痕。

    判例「空集必须显式报警不得静默输出零」的此处用法: 不伪造"通过",
    而是明说"这一闸没能判"。硬底线由闸A 时效兜住 (不依赖外部数据)。
    """
    db, pool, pm = env
    _approve(pm)
    db.get_connection().execute("DELETE FROM daily_klines WHERE code=?", (CODE,))
    db.get_connection().commit()

    with caplog.at_level("WARNING"):
        pm.consume_approved()

    assert pool.get(CODE) is not None, "无价不应拦截 (否则数据断供再造积压)"
    assert any("价格现实" in r.message or "无收盘价" in r.message
               for r in caplog.records), (
        "价格闸未能判定时必须显式留痕, 不得静默放行"
    )


# ---------- 7. 闸A: 陈旧单拒绝, 且不报 rr 理由 ----------

def test_gate_age_rejects_stale_and_does_not_blame_rr(env):
    """09-21 单在 09-25 = 4 个交易日 > 2 → 拒。

    且理由只说陈旧, **不得报 rr/价格结论** —— 陈旧单的 entry 参数本身已
    失效, 拿失效参数算出的 rr 去定性是误导 (这是运行序 A→B→C 的理由)。
    """
    db, pool, pm = env
    _approve(pm, created_date="2026-09-21", take_profit=10.8)  # rr 也不合格
    _set_close(db, 10.5)

    pm.consume_approved()

    assert pool.get(CODE) is None, "陈旧单不得入池"
    reasons = _skip_reasons(db)
    assert any("陈旧" in r or "超期" in r for r in reasons), (
        f"陈旧拒绝必须留痕, 实际: {reasons}"
    )
    assert not any("风险收益比" in r for r in reasons), (
        f"陈旧单不得用失效参数定性 rr, 实际: {reasons}"
    )


# ---------- 8. 闸A 边界: age=2 放行 ----------

def test_gate_age_boundary_two_days_passes(env):
    """09-23 单在 09-25 = 2 个交易日 = 阈值, 放行 (实测 2 日漂出率 0%)。"""
    db, pool, pm = env
    _approve(pm, created_date="2026-09-23")
    _set_close(db, 10.5)

    pm.consume_approved()

    assert pool.get(CODE) is not None, "age=2 在阈值内, 不得误杀"

# ---------- 9/10. rr 阈值浮点边界 (生产实证: 阈值正压在生成器设计点上) ----------

def test_rr_gate_tolerates_float_noise_at_exact_threshold(env):
    """数学上正好 rr=1.5 的单必须放行, 不得被 IEEE754 末位噪声误杀。

    生产实证 (2026-09-25 积压 35 单全精度实算): 上游策略按 rr≈1.5 生成参数,
    35 单里 10 单落在 [1.4999, 1.5001], 其中 3 单因浮点在第 15 位偏低被
    `rr < 1.5` 误判拒绝 —— 601916=1.4999999999999978 / 002091=1.4999999999999984
    / 300832=1.4999999999999947。对照 stock_pool 已入池单 min=1.5000 max=1.5003,
    2/3 紧贴阈值 → **阈值与生成器设计点重合, 浮点末位不该决定生死**。

    本例用 300832 真实参数: mid=(47.06+48.4)/2=47.73,
    (51.75−47.73)/(47.73−45.05) = 4.02/2.68 = 1.5 (数学精确)。
    """
    db, pool, pm = env
    assert pm._rr_ratio(47.06, 48.4, 45.05, 51.75) < 1.5, (
        "前提失效: 该组参数在本机不再产生浮点下溢, 需换一组实证参数"
    )
    _approve(pm, entry_low=47.06, entry_high=48.4, stop_loss=45.05,
             take_profit=51.75)
    _set_close(db, 47.73)

    pm.consume_approved()

    assert pool.get(CODE) is not None, (
        "数学上 rr=1.5 的单被浮点噪声误杀 — 阈值比较需容差"
    )


def test_rr_gate_still_rejects_genuinely_below(env):
    """容差不得放水: 真正低于阈值的单照拒 (002266 实参 rr=1.4015)。"""
    db, pool, pm = env
    _approve(pm, entry_low=4.52, entry_high=4.66, stop_loss=4.32,
             take_profit=4.968)
    _set_close(db, 4.59)

    pm.consume_approved()

    assert pool.get(CODE) is None, "rr=1.40 真低于阈值, 必须拒"
    assert any("风险收益比" in r for r in _skip_reasons(db))

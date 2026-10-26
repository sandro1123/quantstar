# -*- coding: utf-8 -*-
"""AUD-027 守卫 RED — 带持仓的票不许被「到期清理」吃掉。

## 生产实证（2026-09-16 取证，两例同形，有硬日期）

`688029`：09-11 09:35 `entry qty=600 price=72.29` **真买入** → 09-11 17:28 `sell_signal`
→ 09-14 08:05 `skip_exit`（结算跳过，即告警 #106 那批）→ **09-14 16:07 策略轮
`add_to_watch` 把这只已持仓待结算的票当新候选重新入池**，status 被覆写回 `watch`
→ 09-15 全天 `skip_entry` 刷 67 条（系统在反复试图买入一只它已持有 600 股的股票）。

按 `watch_expire_days=7`、`added_date='2026-09-11'`、`cutoff=bj_date()-7`：
**2026-09-19 命中 `expire_stale_watch`**，600 股以 `exit_reason='watch_expired'`、
`exit_price=NULL`、`total_return=NULL` 出场 —— **这笔交易的损益永久丢失**。

`003004` 同形晚一天，但停在 `status='pending_entry'`，走的是**另一条腿**
`expire_stale_pending`（`valid_until<today` → `update_status(code,'watch')`）。
对带仓票，那一步本身就是状态损坏，而且正好把它送进第一条腿。
**所以守卫必须挡两条**——靠「先推回 watch、再由 watch 守卫兜」是脆的：
第一步若改了行为，守卫就落空。防御不能建立在两步链上。

## 这条守卫堵的是我自己那个没写下来的前提

AUD-027 我按「幽灵交易」那个风险面设计：`watch_expired` 的票从未买入，
不该带卖出价/收益（300906 就是这么进统计面的）。**该前提对真正的 watch 票成立。**
但我没把它写下来 —— 于是「watch 态的票没有持仓」成了**隐含假设**。
当持仓因状态互踩落进 watch 态，假设失效，而我的清字段修复会让这笔损失
**以"干净"的形态发生**：旧实现继承陈值，会在统计面留下一行看起来就不对的记录；
我的版本让它清清爽爽地 NULL 出场。**我把那个会让人看见的症状抹掉了。**

→ 判例：**修复的前提若未写进注释，它就不是前提，是隐含假设；而隐含假设会在下一个
风险面上失效。**
"""
import pytest
from common.database import Database
from execution_layer import ExecutionLayer

OLD = "2026-09-01"        # 远早于 cutoff，必命中到期
ALERT_TITLE_FRAGMENT = "持仓"


@pytest.fixture
def el():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    # 夹具须覆盖被测面真正会写的表。N4 甲-1 我就因为只建了主表、没建 alerts
    # 而让 AlertManager 撞 no such table —— 同一个「夹具窄面」坑不重犯。
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, severity TEXT NOT NULL,
            layer TEXT, title TEXT NOT NULL, detail TEXT, action TEXT,
            status TEXT DEFAULT 'open', resolved_at TEXT,
            created_at TEXT DEFAULT (datetime('now','+8 hours')));
    """)
    conn.commit()
    layer = ExecutionLayer(db=db)
    layer.start()
    yield layer
    layer.shutdown()
    db.close()


def _seed(el, code, status, qty, *, added=OLD, valid_until=None):
    """造一行到期清理必然扫到的票。qty>0 = 生产里那两只的形状。"""
    pool = el.stock_pool
    pool.add(code, f"测试{code}")
    conn = pool.db.get_connection()
    conn.execute(
        "UPDATE stock_pool SET status=?, position_qty=?, added_date=?, "
        "entry_price=72.29, entry_date=?, valid_until=? WHERE code=?",
        (status, qty, added, added, valid_until, code))
    conn.commit()
    return pool


def _row(el, code):
    return el.stock_pool.db.get_connection().execute(
        "SELECT status, position_qty, exit_reason, exit_date, entry_price "
        "FROM stock_pool WHERE code=?", (code,)).fetchone()


def _alerts(el):
    return el.stock_pool.db.get_connection().execute(
        "SELECT severity, title, detail FROM alerts ORDER BY id").fetchall()


# ══════════════════════════════════ 腿一：expire_stale_watch

def test_held_watch_is_not_expired(el):
    """**本件核心**：带仓的 watch 票不许被到期清理吃掉（688029 的形状）。"""
    pool = _seed(el, "688029", "watch", 600)
    expired = pool.expire_stale_watch(expire_days=7)

    r = _row(el, "688029")
    assert r["status"] == "watch", \
        f"带 600 股的票被清成 {r['status']} —— 这就是 09-19 会发生的事"
    assert r["exit_reason"] is None, "不许写 watch_expired"
    assert "688029" not in expired, "返回值不许把被守卫拦下的票记成已到期"


def test_unheld_watch_still_expires(el):
    """**反面锚**：正常的无仓 watch 票照常到期。

    先钉死这条，上面那条「拦下来」才有意义 —— 一个「什么都拦」的坏守卫
    也能让所有正面断言通过，代价是把正常清理也废掉。
    """
    pool = _seed(el, "600001", "watch", 0)
    expired = pool.expire_stale_watch(expire_days=7)

    assert "600001" in expired, "无仓票本就该到期，守卫不许误伤"
    assert _row(el, "600001")["status"] == "exited"
    assert _row(el, "600001")["exit_reason"] == "watch_expired"


def test_mixed_batch_expires_only_the_unheld(el):
    """混批：守卫必须逐行判，不是一行有仓就整批放弃。"""
    pool = _seed(el, "688029", "watch", 600)
    _seed(el, "600001", "watch", 0)
    _seed(el, "600002", "watch", 0)

    expired = sorted(pool.expire_stale_watch(expire_days=7))
    assert expired == ["600001", "600002"], expired
    assert _row(el, "688029")["status"] == "watch"


# ══════════════════════════════════ 腿二：expire_stale_pending

def test_held_pending_is_not_pushed_back_to_watch(el):
    """003004 的形状：带仓的 pending_entry 不许被推回 watch。

    **防御不能建立在两步链上** —— 若指望「先推回 watch、再由 watch 守卫兜」，
    那第一步改了行为守卫就落空。这条腿自己挡自己的。
    """
    pool = _seed(el, "003004", "pending_entry", 600, valid_until="2026-09-01")
    expired = pool.expire_stale_pending(today="2026-09-16")

    r = _row(el, "003004")
    assert r["status"] == "pending_entry", f"带仓票被推成 {r['status']}"
    assert "003004" not in expired


def test_unheld_pending_still_expires(el):
    """反面锚：无仓的过期 pending 照常作废回观察池。"""
    pool = _seed(el, "600003", "pending_entry", 0, valid_until="2026-09-01")
    expired = pool.expire_stale_pending(today="2026-09-16")

    assert "600003" in expired
    assert _row(el, "600003")["status"] == "watch"


# ══════════════════════════════════ 告警面：拦下来必须有人知道

def test_guard_raises_alert(el):
    """**拦下来不告警 = 换个地方再犯同一个病。**

    静默地"正确处理"一个本不该发生的状态，就是让根因继续潜伏 ——
    688029 这只已经在 `skip_entry` 刷了 67 条，系统喊了一整天没人听懂；
    守卫若也不出声，它就永远停在"被拦着但没人修"。
    """
    pool = _seed(el, "688029", "watch", 600)
    pool.expire_stale_watch(expire_days=7)

    alerts = _alerts(el)
    assert len(alerts) == 1, f"拦下带仓票必须告警，实得 {len(alerts)} 条"
    assert ALERT_TITLE_FRAGMENT in alerts[0][1]


def test_alert_detail_names_code_and_qty(el):
    """detail 必须点名代码与股数 —— 只说「有票被拦」的告警等于没说。"""
    pool = _seed(el, "688029", "watch", 600)
    pool.expire_stale_watch(expire_days=7)

    detail = _alerts(el)[0][2]
    assert "688029" in detail, "不点名代码，收告警的人无从下手"
    assert "600" in detail, "不报股数，判不出轻重"


def test_alert_title_is_stable_across_counts(el):
    """title 跨 N 稳定 —— AlertManager 靠同名 open 抑制去重（`alert_manager.py:41`）。

    title 里塞数量 → 每天一条新 open → 积压天天涨，正是 AUD-019 的成因。
    """
    pool = _seed(el, "688029", "watch", 600)
    pool.expire_stale_watch(expire_days=7)
    t1 = _alerts(el)[0][1]

    conn = el.stock_pool.db.get_connection()
    conn.execute("DELETE FROM alerts")
    _seed(el, "003004", "watch", 600)
    conn.commit()
    pool.expire_stale_watch(expire_days=7)
    t2 = _alerts(el)[0][1]

    assert t1 == t2, f"title 随数量/代码变了: {t1!r} vs {t2!r}"


def test_clean_batch_sends_no_alert(el):
    """反面：全是无仓票的正常到期不许发告警（发了就是噪音源）。"""
    pool = _seed(el, "600001", "watch", 0)
    pool.expire_stale_watch(expire_days=7)
    assert _alerts(el) == []


def test_alert_failure_does_not_break_expiry_but_is_logged(el, monkeypatch, caplog):
    """fail-safe 但**不静默**：告警通道炸了不许把守卫也带走。

    守卫的职责是"不清掉带仓票"，告警只是附加。二者耦合的话，
    告警一坏，带仓票就又会被清 —— 那是最坏的失败方向。
    """
    import ops_layer.alert_manager as am
    monkeypatch.setattr(am.AlertManager, "send",
                        lambda self, alert: (_ for _ in ()).throw(RuntimeError("probe: 通道故障")))
    pool = _seed(el, "688029", "watch", 600)
    _seed(el, "600001", "watch", 0)

    with caplog.at_level("WARNING"):
        expired = pool.expire_stale_watch(expire_days=7)

    assert _row(el, "688029")["status"] == "watch", "告警失败不许连累守卫"
    assert "600001" in expired, "告警失败不许阻断正常到期"
    assert any("probe" in r.message or "告警" in r.message for r in caplog.records), \
        "告警失败静默 = 换个地方再犯同一个病"


# ══════════════════════════════════ 守卫的守卫

def test_premise_is_written_into_the_code(el):
    """**前提必须写进代码**（统筹裁定的硬要求）。

    AUD-027 的前提「watch 态的票没有持仓」我当时没写下来，于是它成了隐含假设，
    在「真持仓落进 watch」这个风险面上失效。判例：
    **前提若未写进注释，它就不是前提，是隐含假设。**
    锚代码面不是注释面会锚错——这里恰恰**就是**要锚注释，所以显式取源码文本。
    """
    import inspect
    from execution_layer.core.stock_pool import StockPool
    src = inspect.getsource(StockPool.expire_stale_watch)
    assert "position_qty" in src, "守卫本体必须在源码里"
    assert "前提" in src or "假设" in src, "前提没写进代码 = 下一个人还会踩"

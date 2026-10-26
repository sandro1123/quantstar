# -*- coding: utf-8 -*-
"""G1/G2 修复的测试钉（2026-10-22 夜）。

G1: e2e B1「有效成功」正则收窄 —— 只取顶层腿名，不取括号内子原因。
G2: tomorrow_picks 渲染白名单 —— 引用 ALIVE_STATUSES，不再硬编码。
"""
import importlib.util
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _load_e2e():
    spec = importlib.util.spec_from_file_location(
        "e2e_g1", os.path.join(ROOT, "scripts", "e2e_verification.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


E2E = _load_e2e()


def _conn_with_kline(rows=5000, date="2026-10-22"):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE daily_klines (code TEXT, date TEXT)")
    c.executemany("INSERT INTO daily_klines VALUES (?,?)",
                  [("000001", date)] * rows)
    c.commit()
    return c


# ════════ G1: e2e 顶层腿名解析 ════════

def test_nested_sub_reason_not_treated_as_leg():
    """钉1 (G1 核心): 括号内的子原因不得被当成顶层腿名。

    真实 error_msg: "shortterm_collect: partial (anomaly: 无数据 ...)"
    `anomaly` 是 shortterm_collect 的内部原因 ⇒ 不得判「非白名单腿降级」。
    这是 e2e 夜间 10 连败的真因。
    """
    conn = _conn_with_kline()
    err = "shortterm_collect: partial (anomaly: 无数据 (源不可用或非交易日))"
    ok, why = E2E._ddp_effective_success(conn, err, "2026-10-22")
    assert ok is True, f"顶层腿 shortterm_collect 在白名单内应判有效成功, 实得: {why}"
    assert "anomaly" not in why or "非白名单" not in why, (
        f"子原因 anomaly 被误判为腿名: {why}")


def test_top_level_tolerated_leg_still_passes():
    """钉2: 顶层 tolerated 腿（无嵌套）照常判通过。"""
    conn = _conn_with_kline()
    err = "lhb_daily: partial (当日未发布); index_daily: partial (缺少业务日)"
    ok, why = E2E._ddp_effective_success(conn, err, "2026-10-22")
    assert ok is True, f"两条 tolerated 顶层腿应判通过, 实得: {why}"


def test_top_level_critical_leg_still_fails():
    """钉3 (反向): 顶层 critical 腿降级仍必须判 fail —— 收窄不能放过真红。"""
    conn = _conn_with_kline()
    err = "kline: partial (某源失败)"
    ok, why = E2E._ddp_effective_success(conn, err, "2026-10-22")
    assert ok is False, f"critical 腿 kline 降级必须判 fail, 实得 ok={ok} {why}"
    assert "critical" in why.lower() or "kline" in why


def test_top_level_unknown_leg_still_fails():
    """钉4 (反向): 顶层的非白名单腿仍必须判 fail。"""
    conn = _conn_with_kline()
    err = "some_unknown_leg: partial (原因)"
    ok, why = E2E._ddp_effective_success(conn, err, "2026-10-22")
    assert ok is False, f"非白名单顶层腿必须判 fail, 实得 ok={ok}"
    assert "非白名单" in why


# ════════ G2: 渲染白名单 ════════

def _pool_db(statuses):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE stock_pool (
        code TEXT, name TEXT, status TEXT, entry_low REAL, entry_high REAL,
        stop_loss REAL, take_profit REAL, initial_score REAL,
        added_reason TEXT, valid_until TEXT)""")
    for i, st in enumerate(statuses):
        c.execute("INSERT INTO stock_pool VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (f"60000{i}", "测试", st, 1.0, 2.0, 0.9, 2.2,
                   1.0 - i * 0.1, "理由", "2026-10-29"))
    c.commit()
    return c


def test_render_query_includes_active_status():
    """钉5 (G2 核心): 渲染查询必须覆盖 ALIVE_STATUSES 全部状态，含 active。

    当日病: 库内 2 只 status=active，旧 SQL 只认 watch/pending_entry ⇒ 渲染空。
    """
    from execution_layer.core.stock_pool import ALIVE_STATUSES
    assert "active" in ALIVE_STATUSES, "ALIVE_STATUSES 必须含 active"

    conn = _pool_db(["active", "watch", "pending_entry", "exited"])
    alive = tuple(sorted(ALIVE_STATUSES))
    ph = ",".join("?" * len(alive))
    rows = conn.execute(
        f"SELECT code FROM stock_pool WHERE status IN ({ph})", alive).fetchall()
    assert len(rows) == 3, (
        f"active/watch/pending_entry 三只都应被查到, 实得 {len(rows)} 只")


def test_render_sql_source_uses_alive_constant():
    """钉6 (守卫钉): tomorrow_picks 的 SQL 必须引用 ALIVE_STATUSES，
    不得再硬编码状态白名单（防后人在另一个地方漏掉新状态）。"""
    import inspect
    from report_layer.sections import tomorrow_picks
    src = inspect.getsource(tomorrow_picks)
    assert "ALIVE_STATUSES" in src or "_ALIVE" in src, (
        "tomorrow_picks 未引用 ALIVE_STATUSES —— 状态白名单会再次与权威定义脱节")
    # 反向: 不得出现硬编码的 watch/pending_entry 元组
    assert "('watch', 'pending_entry')" not in src, (
        "tomorrow_picks 仍硬编码 ('watch','pending_entry')")


# C 卡修订 (2026-10-24): 原规「所有报告腿必须带 config_key」写得太绝对 ——
# 它把手段(带键)当成了目的(配置能生效), 漏了前提「键必须有宿主」。
# G3 当时为满足自己那条无条件规, 给两条无宿主的腿硬造了不存在的键
# (ops.pipelines.intraday_alert / alerts) = 假接线(改了不生效, 比不可配更坏)。
#
# 修订后的规 (补上缺失的前提, 而非加豁免绕过):
#   有管道宿主的腿 → 必须带 config_key (配置必须能生效)
#   无管道宿主的腿 → 必须不带 config_key, 且必须写明理由注释
#   (造一个没人读的键 = 假接线)
_NO_HOST_LEGS = {"build_intraday_alert", "build_alerts"}
# 无宿主腿必须写明的理由语 (防后人以为"只是忘了接"又给接上)
_NO_HOST_REASON = "无独立管道宿主"


def test_no_report_leg_hardcodes_60s_budget():
    """G3 守卫(经 C 卡修订): 报告腿预算必须 config-driven, 但键必须有宿主。

    背景 (2026-10-22 夜): 晨报 B9 改成了 180s 读配置, 但其余 9 条报告腿
    仍硬编码 60s。实测晚报 build 在 60s 超时(hy3 reasoning 慢)。

    真病: **同一个类的 10 个方法, 只给 1 个改对了** —— 改的是"我看到的那个",
    不是"所有同类"。这与 B9 刀2 假绿同族(判据与生效点不同源)。

    本钉 (2026-10-24 修订):
      1 report_layer 不得出现硬编码 timeout_s=60
      2 有管道宿主的报告腿必须带 config_key
      3 无管道宿主的报告腿必须**不带** config_key, 且写明「无独立管道宿主」
        (无宿主还带键 = 假接线; 这条同时防后人"帮它接上")
    """
    import os
    import re

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))),
        "src", "report_layer", "__init__.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()

    hard = re.findall(r"@with_timeout\(timeout_s=60\)", src)
    assert not hard, (
        f"报告层仍有 {len(hard)} 处硬编码 timeout_s=60 —— "
        "hy3 reasoning 慢, 60s 会截断; 应改为 config_key 驱动")

    legs = re.findall(r"def (build_\w+|_build_report)\(", src)
    assert legs, "未找到报告腿"

    for leg in legs:
        m = re.search(rf"def {leg}\(", src)
        upto = src[:m.start()]
        last_to = None
        for mm in re.finditer(r"@with_timeout\(([^)]*)\)", upto):
            last_to = mm.group(1)
        assert last_to is not None, f"{leg} 上方未找到 with_timeout"

        if leg in _NO_HOST_LEGS:
            # 无宿主腿: 必须不带 config_key, 且必须有理由注释
            assert "config_key" not in last_to, (
                f"{leg} 无独立管道宿主却带了 config_key —— "
                f"造一个没人读的键 = 假接线(改了不生效, 比不可配更坏); "
                f"实际: {last_to}")
            # 理由注释必须出现在该腿上方 (取该腿前面那段注释块)
            head = src[:m.start()]
            # 从该腿的装饰器往上回溯连续注释行
            lines = head.splitlines()
            block = []
            for ln in reversed(lines):
                s = ln.strip()
                if s.startswith("#"):
                    block.append(s)
                elif s.startswith("@") or s == "":
                    continue
                else:
                    break
            reason_blob = "\n".join(block)
            assert _NO_HOST_REASON in reason_blob, (
                f"{leg} 属无宿主腿，必须写明「{_NO_HOST_REASON}」的理由注释 —— "
                f"否则后人会以为它只是忘了接配置, 又给接上; "
                f"实际注释块: {reason_blob[:120]!r}")
        else:
            assert "config_key" in last_to, (
                f"{leg} 的 with_timeout 缺 config_key (实际: {last_to})")

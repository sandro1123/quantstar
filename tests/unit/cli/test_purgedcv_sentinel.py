# F1 purgedcv 升级哨兵施工 (只读)
"""F1 purgedcv 升级哨兵测试 (D-291 期1配套, 统筹 sm-58814c13363f4078 施工卡)。

设计稿: 分层讨论/F1F2F3期1配套施工设计稿-统筹-20260903.md (md5 09b67bae)
§2 语义面:
  1. 前置熄灭: state != lightweight (strict/keep_light) → 永远空 (已升级/用户明裁);
  2. T1 触发: walk_forward OOS 累计 total_trades >= oos_trade_threshold (50);
  3. T2 触发: stable_window_end 非 null 且 当日 >= 窗末日 且
     EXISTS walk_forward run created_at >= 窗末日 (窗后首考已发生);
  4. 先到先触发; 触发后每晚重报直到 state 翻转 (render-time 无状态重算=防忘本体);
  5. severity=warning; dedup_key 固定 purgedcv-upgrade-sentinel (跨夜同键);
  6. 旧库无 backtest_runs 表 → 无 issue 不炸 (外层 _collect_all 已有降级)。

T1 计数口径钉死 (设计稿 §1.2):
  SELECT COALESCE(SUM(total_trades),0) FROM backtest_runs WHERE mode='walk_forward'
  — daily_backtest/hold 不入计数 (宁可少算不虚涨)。

配置面: ops.yaml 顶层段 purged_cv_upgrade 三键 (state/oos_trade_threshold/
stable_window_end), 顶层段全键空域读取 — 禁 pipeline-block-scope (D-293 陷阱面)。
缺 state 键 = lightweight 默认 (同义)。
"""
import os
import sqlite3

import pytest

from cli.issues import collect_purgedcv_upgrade_sentinel

SCHEMA = """
CREATE TABLE backtest_runs (
    run_id TEXT, mode TEXT, total_trades INTEGER, created_at TEXT);
"""

D = "2026-09-03"          # 当日 (T2 比较左值)
THRESH = 50               # 设计稿 §2.1 oos_trade_threshold

def _mk(tmp_path, wf_trades=9, wf_created="2026-08-30 20:00:57",
        hold_trades=0, daily_trades=0):
    """夹具: walk_forward 单跑 + hold/daily 干扰行 (不入计数口径断言)。"""
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.executescript(SCHEMA)
    if wf_trades > 0:
        conn.execute(
            "INSERT INTO backtest_runs VALUES ('wf1','walk_forward',?,?)",
            (wf_trades, wf_created))
    if hold_trades:
        conn.executemany(
            "INSERT INTO backtest_runs VALUES (?,'hold',?,'2026-09-02 16:56:09')",
            [(f"h{i}", hold_trades) for i in range(3)])
    if daily_trades:
        conn.execute(
            "INSERT INTO backtest_runs VALUES ('db1','daily_backtest',?,"
            "'2026-09-02 16:40:00')", (daily_trades,))
    conn.commit()
    return conn

# ── 1. 前置熄灭: 三 state 熄灭语义 ──────────────────────────────

def test_state_strict_extinguished(tmp_path):
    """state=strict (已升级) → 空, 即便 T1/T2 均已满足。"""
    conn = _mk(tmp_path, wf_trades=51)
    issues, _ = collect_purgedcv_upgrade_sentinel(conn, D,
                                                  state="strict")
    assert issues == []
    conn.close()

def test_state_keep_light_extinguished(tmp_path):
    """state=keep_light (用户明裁保留轻量) → 静默留档, T1 满足也不报。"""
    conn = _mk(tmp_path, wf_trades=51)
    issues, _ = collect_purgedcv_upgrade_sentinel(conn, D,
                                                  state="keep_light")
    assert issues == []
    conn.close()

def test_state_missing_defaults_lightweight_active(tmp_path):
    """缺 state 键 = lightweight 默认 (同义) → 哨兵活性面在。"""
    conn = _mk(tmp_path, wf_trades=51)
    issues, _ = collect_purgedcv_upgrade_sentinel(conn, D, state=None)
    assert len(issues) == 1
    conn.close()

# ── 2. T1 触发: 9 vs 51 两态 ────────────────────────────────────

def test_t1_below_threshold_no_issue(tmp_path):
    """当前生产原态: 9 < 50 → 无 issue (负向冒烟夹具面)。"""
    conn = _mk(tmp_path, wf_trades=9)
    issues, asof = collect_purgedcv_upgrade_sentinel(conn, D,
                                                     state="lightweight")
    assert issues == []
    assert asof is None
    conn.close()

def test_t1_at_threshold_triggers(tmp_path):
    """恰好达阈 (50 == 50) → 触发 (>= 语义)。"""
    conn = _mk(tmp_path, wf_trades=50)
    issues, _ = collect_purgedcv_upgrade_sentinel(conn, D,
                                                  state="lightweight")
    assert len(issues) == 1
    conn.close()

def test_t1_above_threshold_triggers(tmp_path):
    """51 >= 50 → 触发; 条目形态全断言 (设计稿 §2.2)。"""
    conn = _mk(tmp_path, wf_trades=51)
    issues, _ = collect_purgedcv_upgrade_sentinel(conn, D,
                                                  state="lightweight")
    assert len(issues) == 1
    it = issues[0]
    assert it["severity"] == "warning"
    assert it["source"] == "purgedcv_upgrade_sentinel"
    assert it["dedup_key"] == "purgedcv-upgrade-sentinel"   # 跨夜固定键
    assert "51" in it["title_zh"] and "50" in it["title_zh"]
    assert it["evidence"]["oos_trades"] == 51
    assert it["evidence"]["oos_trade_threshold"] == 50
    assert it["evidence"]["trigger"] == "t1_trades"
    conn.close()

def test_t1_hold_and_daily_not_counted(tmp_path):
    """口径钉死: hold/daily_backtest 大量 trades 不虚涨计数 (宁可少算)。"""
    conn = _mk(tmp_path, wf_trades=9, hold_trades=30, daily_trades=500)
    issues, _ = collect_purgedcv_upgrade_sentinel(conn, D,
                                                  state="lightweight")
    assert issues == []   # 9 + 30×3 + 500 仍不触发 — 只认 walk_forward
    conn.close()

# ── 3. T2 触发: 稳定窗日期态全组合 ─────────────────────────────

def test_t2_window_end_null_no_trigger(tmp_path):
    """窗末日 null (F2 落键前, 当前产态) → T2 无从触发。"""
    conn = _mk(tmp_path, wf_trades=9)
    issues, _ = collect_purgedcv_upgrade_sentinel(
        conn, D, state="lightweight", stable_window_end=None)
    assert issues == []
    conn.close()

def test_t2_before_window_end_no_trigger(tmp_path):
    """当日 < 窗末日 (窗内) → 不触发, 即便窗后考试行存在。"""
    conn = _mk(tmp_path, wf_trades=9,
               wf_created="2026-09-20 20:00:00")  # 窗后考试行 (提前落库)
    issues, _ = collect_purgedcv_upgrade_sentinel(
        conn, "2026-09-10", state="lightweight",
        stable_window_end="2026-09-17")
    assert issues == []
    conn.close()

def test_t2_at_window_end_without_exam_no_trigger(tmp_path):
    """当日 == 窗末日 但窗后无 walk_forward 考试行 → 未首考不触发。"""
    conn = _mk(tmp_path, wf_trades=9,
               wf_created="2026-08-30 20:00:57")  # 窗前旧跑, 不算首考
    issues, _ = collect_purgedcv_upgrade_sentinel(
        conn, "2026-09-17", state="lightweight",
        stable_window_end="2026-09-17")
    assert issues == []
    conn.close()

def test_t2_after_window_end_with_exam_triggers(tmp_path):
    """窗末已过 + 窗后首考已发生 → T2 触发 (trigger=t2_window)。"""
    conn = _mk(tmp_path, wf_trades=9,
               wf_created="2026-09-20 20:00:00")  # 窗后首考行
    issues, _ = collect_purgedcv_upgrade_sentinel(
        conn, "2026-09-21", state="lightweight",
        stable_window_end="2026-09-17")
    assert len(issues) == 1
    assert issues[0]["evidence"]["trigger"] == "t2_window"
    assert issues[0]["evidence"]["stable_window_end"] == "2026-09-17"
    conn.close()

def test_t2_exam_uses_created_at_not_run_date(tmp_path):
    """首考判定读 created_at >= 窗末日 (非 run 日期字段 — 表无此列, 钉住口径)。"""
    conn = _mk(tmp_path, wf_trades=9,
               wf_created="2026-09-18 16:00:00")
    issues, _ = collect_purgedcv_upgrade_sentinel(
        conn, "2026-09-21", state="lightweight",
        stable_window_end="2026-09-17")
    assert len(issues) == 1
    assert issues[0]["evidence"]["trigger"] == "t2_window"
    conn.close()

# ── 4. 先到先触发 + 每晚重报 ──────────────────────────────────

def test_both_triggers_t1_wins(tmp_path):
    """T1/T2 同时满足 → 先到先触发报 T1 (设计稿 §2.2)。"""
    conn = _mk(tmp_path, wf_trades=51,      # T1 满足
               wf_created="2026-09-20 20:00:00")  # T2 也满足
    issues, _ = collect_purgedcv_upgrade_sentinel(
        conn, "2026-09-21", state="lightweight",
        stable_window_end="2026-09-17")
    assert len(issues) == 1
    assert issues[0]["evidence"]["trigger"] == "t1_trades"
    conn.close()

def test_retrigger_nightly_same_dedup(tmp_path):
    """防忘本体: 触发条件仍在 → 每晚重报, dedup_key 跨夜不变 (台账幂等只推一条线)。"""
    conn = _mk(tmp_path, wf_trades=51)
    for day in ("2026-09-03", "2026-09-04", "2026-09-05"):
        issues, _ = collect_purgedcv_upgrade_sentinel(conn, day,
                                                      state="lightweight")
        assert len(issues) == 1
        assert issues[0]["dedup_key"] == "purgedcv-upgrade-sentinel"
    conn.close()

# ── 5. 健壮性: 旧库/空库/配置缺省 ──────────────────────────────

def test_old_db_no_table_no_crash(tmp_path):
    """旧库无 backtest_runs 表 → 无 issue 不炸 (不是回归; 异常另有外层降级)。"""
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.execute("CREATE TABLE other (x TEXT)")
    issues, asof = collect_purgedcv_upgrade_sentinel(conn, D,
                                                    state="lightweight")
    assert issues == []
    assert asof is None
    conn.close()

def test_empty_runs_table_no_issue(tmp_path):
    """空 backtest_runs (零跑历史) → COALESCE 0 < 50, 无 issue 不炸。"""
    conn = _mk(tmp_path, wf_trades=0)
    issues, _ = collect_purgedcv_upgrade_sentinel(conn, D,
                                                  state="lightweight")
    assert issues == []
    conn.close()

def test_threshold_from_config_default_50(tmp_path):
    """阈值参数化: 显式传 30 → 30 笔即触发 (键值即唯一事实源, 统筹可调)。"""
    conn = _mk(tmp_path, wf_trades=30)
    issues, _ = collect_purgedcv_upgrade_sentinel(
        conn, D, state="lightweight", oos_trade_threshold=30)
    assert len(issues) == 1
    assert issues[0]["evidence"]["oos_trade_threshold"] == 30
    conn.close()

# ── 6. 注册 + 配置段 ───────────────────────────────────────────

def test_registered_in_collectors():
    """挂 COLLECTORS 名册 (注册即每晚 21:07 自动出现=防忘形态本体)。"""
    from cli.issues import COLLECTORS
    names = [n for n, _ in COLLECTORS]
    assert "purgedcv_upgrade_sentinel" in names

def test_ops_yaml_top_level_segment():
    """ops.yaml 顶层段 purged_cv_upgrade 三键在位 (全键空域; 非 ops 内嵌)。"""
    import re
    root = __file__
    for _ in range(4):   # tests/unit/cli/x.py → repo 根 (4 层)
        root = os.path.dirname(root)
    path = os.path.join(root, "config", "defaults", "ops.yaml")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    # 顶层段: 行首无缩进 (禁 pipeline-block-scope / ops 内嵌 — D-293 陷阱)
    assert re.search(r"^purged_cv_upgrade:\s*$", text, re.M), \
        "ops.yaml 缺顶层段 purged_cv_upgrade (须行首无缩进)"
    m = re.search(r"^purged_cv_upgrade:\s*\n((?:[ \t]+.*\n?)*)", text, re.M)
    body = m.group(1)
    assert re.search(r"^  state:\s*lightweight\s*$", body, re.M)
    assert re.search(r"^  oos_trade_threshold:\s*50\s*$", body, re.M)
    assert re.search(r"^  stable_window_end:\s*(?:null|~|\"\d{4}-\d{2}-\d{2}\")\s*$", body, re.M)

# ── 7. 同构断言: _purgedcv_cfg ≜ loader.load_profile ──────────────
# 统筹裁定 sm-bcacc18baf3f4a48 (#96 先例同构: 合并序对拍从「统筹窗一次性
# 验」升格「常驻测试锁」)。预期失败模式: 本测试红 = loader.py 合并序
# 已变而哨兵镜像读未跟 = 哨兵读面漂移告警 —— 这正是它存在的意义
# (红即告警非测试病; 修法=同步 _purgedcv_cfg 合并序, 或落
# 「合并序常量抽取到共享位」微卡)。

def test_cfg_isomorphism_with_loader(tmp_path, monkeypatch):
    """四层梯同构: _purgedcv_cfg 有效值 ≜ load_profile 同目录有效值。

    一条测试锁两面:
      1. 优先级序 — 有效值 == local 层值 (local > profiles/{profile}
         > settings > defaults-sorted), 逐层异值使任一层序颠倒即红;
      2. 同构性 — 哨兵自持 yaml 读面与 config_layer 真源读面在同一
         目录读出同一组值 (镜像若漂移即红)。

    只走 loader.load_profile 直取, 不碰 Config.load() — 后者=CF-01
    七键炸点 (prod 启动校验 RuntimeError), 断言面引它=断言自身不可跑。
    """
    from cli.issues import _purgedcv_cfg
    from config_layer.loader import ConfigLoader
    from config_layer.registry import ConfigRegistry

    def _w(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    seg = ("purged_cv_upgrade:\n"
           "  state: {}\n"
           "  oos_trade_threshold: {}\n"
           "  stable_window_end: '{}'\n")  # 日期带引号: 免 yaml 解析成 date 型

    # 四层梯: 同键逐层异值 (defaults 两文件兼锁层内 sorted 序)
    cfg = tmp_path / "cfg"
    _w(cfg / "defaults" / "00_base.yaml",
       seg.format("defaults_1st", 11, "2031-01-01"))
    _w(cfg / "defaults" / "99_over.yaml",
       seg.format("defaults_2nd", 22, "2031-01-02"))
    _w(cfg / "settings.yaml",
       seg.format("settings_layer", 33, "2031-01-03"))
    _w(cfg / "profiles" / "prod.yaml",
       seg.format("profile_layer", 44, "2031-01-04"))
    _w(cfg / "local.yaml",
       seg.format("local_layer", 55, "2031-01-05"))

    reg = ConfigRegistry()
    ConfigLoader().load_profile(reg, "prod", str(cfg))
    loader_cfg = {"state": reg.get("purged_cv_upgrade.state"),
                  "oos_trade_threshold":
                      reg.get("purged_cv_upgrade.oos_trade_threshold"),
                  "stable_window_end":
                      reg.get("purged_cv_upgrade.stable_window_end")}

    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    monkeypatch.setenv("CONFIG_ENV", "prod")
    sent_cfg = _purgedcv_cfg()

    # 优先级面: 有效值 == local 层值 (末层胜; 任一层序颠倒即红)
    assert loader_cfg == {"state": "local_layer", "oos_trade_threshold": 55,
                          "stable_window_end": "2031-01-05"}
    # 同构面: 两读面同目录读出同值 (镜像漂移即红)
    assert sent_cfg == loader_cfg, (
        f"哨兵读面漂移: _purgedcv_cfg={sent_cfg} vs load_profile="
        f"{loader_cfg} — loader 合并序已变, _purgedcv_cfg 镜像未跟 "
        "(本测试存在的意义即此告警)")

    # defaults 层内 sorted 序: 无后层覆盖时 99 后写胜 00
    donly = tmp_path / "donly"
    _w(donly / "defaults" / "00_first.yaml",
       seg.format("alpha", 1, "2031-02-01"))
    _w(donly / "defaults" / "99_second.yaml",
       seg.format("beta", 2, "2031-02-02"))
    reg2 = ConfigRegistry()
    ConfigLoader().load_profile(reg2, "prod", str(donly))
    monkeypatch.setenv("CONFIG_DIR", str(donly))
    sent2 = _purgedcv_cfg()
    assert sent2 == {"state": "beta", "oos_trade_threshold": 2,
                     "stable_window_end": "2031-02-02"}
    assert reg2.get("purged_cv_upgrade.state") == "beta"

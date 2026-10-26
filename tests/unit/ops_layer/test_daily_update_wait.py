# -*- coding: utf-8 -*-
"""甲件 A: daily_update 锁等待配置契约 (2026-09-25)。

病: daily_update 是一片"都会等锁"的兄弟腿中间唯一的**孤儿固定时点**——
既无 wait_timeout_s 也无 depends_on, 09-21~24 连续四个交易日 skipped_lock,
35 单批准积压零告警。

正反六条腿实证「wait_timeout_s 是否 > 上游实际持锁时长 = 这条腿活还是死」:
  活: factor_update/daily_backtest/paper_trading = 7200
  死: emotion_cycle=300 (末次成功 09-18) / news_per_stock=600 (末次成功 09-14)
  死得最彻底: daily_update = 无键 (连 wait_timeout 事件都没有, 只有 skipped_lock)

修法 A (统筹 09-25 裁): 只加 wait_timeout_s=7200 止血, **不加 depends_on**
(已证 daily_update 是 cron 直驱脚本不进 orchestrator, depends_on 对它是摆设;
宁可不加也不加摆设)。
"""
from pathlib import Path

import pytest
import yaml

OPS_YAML = Path(__file__).resolve().parents[3] / "config" / "defaults" / "ops.yaml"
KEY = "daily_update"


@pytest.fixture(scope="module")
def cfg():
    with open(OPS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _flock_wait_s(cfg, key):
    """复刻 flock_guard.sh 内嵌的读取逻辑 (单一事实源: 它只读这一处)。"""
    pipes = (cfg.get("ops", {}) or {}).get("pipelines", {}) or {}
    wt = (pipes.get(key, {}) or {}).get("wait_timeout_s")
    return int(wt) if wt is not None else 0


def _watchdog_budget(cfg, key):
    """复刻 cron_watchdog.sh 内嵌的预算派生 (含 if key in pipes 分支)。"""
    ops = cfg.get("ops", {}) or {}
    wd = ops.get("cron_watchdog", {}) or {}
    grace = int(wd.get("grace_s", 120))
    default_t = int(wd.get("default_timeout_s", 300))
    pipes = ops.get("pipelines", {}) or {}
    if key in pipes:
        p = pipes[key]
        t = int(p.get("timeout_s", default_t))
        retries = int(p.get("max_retries", 0))
        delay = int(p.get("retry_delay_s", 30))
        has_deps = bool(p.get("depends_on"))
        ceiling = t * (retries + 1) + delay * retries
    else:
        t = int((wd.get("overrides", {}) or {}).get(key, default_t))
        has_deps = False
        ceiling = t
    return max(ceiling, 900 if has_deps else 0) + grace


def test_daily_update_has_wait_timeout_7200(cfg):
    """止血核心: flock_guard 必须读到 7200 (它只读 ops.pipelines.<key>)。"""
    assert _flock_wait_s(cfg, KEY) == 7200, (
        f"flock_guard 读到的 WAIT_S={_flock_wait_s(cfg, KEY)} != 7200 —— "
        f"注意它只读 ops.pipelines.daily_update.wait_timeout_s, "
        f"写在 cron_watchdog.overrides 里不算"
    )


def test_daily_update_wait_matches_sibling_legs(cfg):
    """同族惯例: 与三条活腿同值, 不另发明数字。"""
    pipes = cfg["ops"]["pipelines"]
    siblings = [pipes[n].get("wait_timeout_s")
                for n in ("factor_update", "daily_backtest", "paper_trading")]
    assert set(siblings) == {7200}, f"同族腿值漂移了: {siblings}"
    assert _flock_wait_s(cfg, KEY) == siblings[0]


def test_daily_update_has_no_depends_on(cfg):
    """**不得加 depends_on** —— 已证是摆设 (daily_update 走 cron 直驱脚本,
    不经 orchestrator, can_run/wait_for_dependencies 对它不生效)。
    加一个不生效的依赖键 = 「看着像防线其实不是」。
    """
    p = cfg["ops"]["pipelines"].get(KEY, {})
    assert "depends_on" not in p, (
        "daily_update 不得配 depends_on: 它是 cron 直驱脚本, orchestrator "
        "的依赖门对它不生效, 加了是摆设"
    )


def test_watchdog_budget_unchanged_by_intent(cfg):
    """加段会把预算分支从 overrides 翻到 pipelines —— 必须**显式**保持 420s。

    裸加 wait_timeout_s (不写 timeout_s) 也恰好得 420s, 但那是
    default_timeout_s(300) 巧合等于 overrides(300), **是巧合不是设计**。
    故本测试锁 timeout_s 显式在位。
    """
    p = cfg["ops"]["pipelines"].get(KEY, {})
    assert "timeout_s" in p, (
        "加了 pipelines 段就必须显式写 timeout_s —— 否则预算靠 default 巧合"
    )
    assert _watchdog_budget(cfg, KEY) == 420, (
        f"预算被改变: {_watchdog_budget(cfg, KEY)}s != 420s (现状)"
    )


def test_superseded_override_not_left_as_dead_duplicate(cfg):
    """加段后 cron_watchdog.overrides.daily_update 不再被读 = 死配置。

    判例「重复定义点 banned」: 死配置是漂移源 —— 后人改它会以为生效。
    要么删掉, 要么留注释说明已被取代 (本测试只断言不能是"无声的活着")。
    """
    ov = (cfg["ops"].get("cron_watchdog", {}) or {}).get("overrides", {}) or {}
    if KEY in ov:
        raw = open(OPS_YAML, encoding="utf-8").read()
        idx = raw.find(f"{KEY}: {ov[KEY]}")
        window = raw[max(0, idx - 400):idx] if idx > 0 else ""
        assert ("取代" in window or "已失效" in window or "superseded" in window
                or "不再" in window), (
            "cron_watchdog.overrides.daily_update 已被 ops.pipelines 段取代但"
            "无任何说明 —— 死配置无声活着, 后人会以为改它有效"
        )

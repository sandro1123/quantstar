# -*- coding: utf-8 -*-
"""#16 cron flock 覆盖 — 扫描式守护

Issue #16 原文:
  「deploy/quantstar-crontab 中仍有约 19 条任务未走 flock_guard.sh。
    逐条确认是否写库, 写库的统一加锁。」

判据不是「全都加锁」而是「**写库的必须加锁, 不写库的必须登记理由**」——
所以本测试用**白名单 + 理由**双钉:
  1. 每条无 flock 的行, 必须在 EXEMPT 白名单里且写明理由;
  2. 白名单里的条目不得凭空多出 (防止豁免筐变成免责筐 — 判例 #23);
  3. 写库类管道必须真的带 flock_guard (正向钉)。

扩充白名单前请先在 docs/cron-flock-coverage.md 登记硬证据。
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CRONTAB = REPO / "deploy" / "quantstar-crontab"

# ── 豁免白名单: 键 → 硬理由 (理由必须是可核的证据, 不是"只读"两字) ──
EXEMPT = {
    "build_morning_brief": "--dry-run 只读, 不写库",
    "build_midday_brief": "--dry-run 只读, 不写库",
    "pre_market_refresh": "只跑 DataReadiness.check() 不落库; AST 无 INSERT/UPDATE",
    "e2e_verification": "只读验证, 写 jsonl 文本日志非 DB; 不产 pipeline_runs 行",
    "log_rotation": "纯文件操作; _run_log_rotation AST 无任何 DB 调用",
    "disk_check": "只读 statvfs; 唯一写是恢复时 resolve 告警(幂等+自带去重), 每小时一次无竞争",
    "api_health_check": "只读 HTTP 探活 + jsonl; 每5分钟一次, 加锁会与同频 intraday_scan 抢锁",
}

# ── 必须走 flock 的键 (写库) ──
# 只登记**开源快照里真实存在**的行 — 作者私有 crontab 里的额外条目
# (paper_trading / kline / shortterm_recollect 等) 不在本文件管辖范围,
# 强行登记会让测试因"该行不存在"而红, 那是误伤不是守护。
MUST_LOCK = [
    "morning_brief", "morning_review", "midday_brief",
    "weekly_summary", "monthly_report",
    "auction_parse", "param_tuning", "wfo_gate", "model_retrain",
    "db_backup",
    # 既有已加锁的写库腿 (回归位, 防有人手抖拆掉)
    "daily_data_pipeline", "factor_update", "fwd_return_backfill", "ic_feedback",
    "daily_backtest", "quality_check", "freshness_audit",
    "evening_report", "dev_verify_report", "daily_ops_report", "health_check",
    "end_of_day", "daily_update",
]


def _rows():
    """解析 crontab → [(用于识别的键, 是否带 flock, 原始行)]。"""
    out = []
    for raw in CRONTAB.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if not re.match(r"^[\d*/,\-]+\s", s):
            continue                      # 非 cron 行
        m = re.search(r"scripts/cron_watchdog\.sh\s+(\w+)", s)
        key = m.group(1) if m else None
        out.append((key, "flock_guard.sh" in s, s))
    return out


class TestFlockCoverage:
    def test_crontab_exists(self):
        assert CRONTAB.exists(), "缺 deploy/quantstar-crontab"

    def test_every_unlocked_line_is_whitelisted_with_reason(self):
        """每条无 flock 的行必须在豁免白名单, 且有非空理由。

        反向钉: 新增一条裸行 → 本测试报红, 强制作者先想"这条写不写库"。
        """
        offenders = []
        for key, has_flock, raw in _rows():
            if has_flock or key is None:
                continue
            if key not in EXEMPT:
                offenders.append(raw[:120])
            elif not EXEMPT[key].strip():
                offenders.append(f"{key}: 白名单理由为空")
        assert not offenders, (
            "以下 cron 行未走 flock_guard 且未登记豁免理由:\n  "
            + "\n  ".join(offenders)
            + "\n→ 若写库请加 flock; 若确不写库, 在 EXEMPT 补理由"
              " (硬证据必须能复核, 不得只写'只读')")

    def test_whitelist_has_no_stale_entries(self):
        """白名单不得含 crontab 里已不存在的键 (防豁免筐变成免责筐 — 判例#23)"""
        present = {k for k, _, _ in _rows() if k}
        stale = set(EXEMPT) - present
        assert not stale, f"白名单里有 crontab 已不存在的键: {stale} — 请清理"

    @pytest.mark.parametrize("key", MUST_LOCK)
    def test_write_pipelines_have_flock(self, key):
        """写库管道必须真带 flock_guard (正向钉)"""
        hits = [(has, raw) for k, has, raw in _rows() if k == key]
        assert hits, f"{key} 不在 crontab 中 (是否改名/移除?)"
        assert all(has for has, _ in hits), (
            f"{key} 有 {sum(1 for h, _ in hits if not h)} 条未加 flock — 它会写库")

    def test_exempt_and_mustlock_disjoint(self):
        """同一条不能既在豁免白名单又在必加锁列表 (自相矛盾)"""
        assert not (set(EXEMPT) & set(MUST_LOCK)), (
            f"矛盾登记: {set(EXEMPT) & set(MUST_LOCK)}")

    def test_flock_key_matches_watchdog_key(self):
        """flock 的锁键必须与外层 watchdog 键一致 (否则锁错对象)"""
        bad = []
        for key, has_flock, raw in _rows():
            if not has_flock:
                continue
            m = re.search(r"flock_guard\.sh\s+(\w+)\s+scripts/cron_watchdog\.sh\s+(\w+)", raw)
            if m and m.group(1) != m.group(2):
                bad.append(f"flock={m.group(1)} watchdog={m.group(2)}")
        assert not bad, "flock 锁键与 watchdog 键不一致:\n  " + "\n  ".join(bad)

    def test_total_coverage_improved(self):
        """总量钉: 至少 31 行带锁 (本卡基线), 防止回归"""
        n = sum(1 for _, has, _ in _rows() if has)
        assert n >= 31, f"带锁行数回退到 {n} (< 31) — 有人在拆锁"

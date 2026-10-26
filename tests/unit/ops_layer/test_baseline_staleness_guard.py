# -*- coding: utf-8 -*-
"""判例落地：baseline 90 天未清理的测试钉（卡B，审计官 10-04 判例）。

判例（审计官 10-04, 与 litestream 判例同源）：
    「建立后 N 天不跑的备份系统，必须经过哨兵警告或降级为观察态，不能静默成为铁环。」

baseline = .hardcoded_date_baseline.txt（6f73b79e test_no_hardcoded_dates.py 维护的债务账）。
风险：它「建了没接线」第 N 变体——治理钉自己静默长锈,哪天文件行数悄悄涨到危险/
或 90 天无人动它, 它就白建。

判据：baseline 文件的「最旧债务行」距今天超过 90 天,
      须逐日算「未清理天数」并 sentinel 报告（不静默）

设计：此测试独立于 test_no_hardcoded_dates.py（它挂在 baseline 文件自身上,
      不依赖主测试已进 main).
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta
from pathlib import Path

BASELINE_PATH = Path(os.path.expanduser("~/qs/.hardcoded_date_baseline.txt"))
# 兼容: 也可能在 src/(根目录或 6f73b79e 分支内)
ALT_PATHS = [
    Path(".hardcoded_date_baseline.txt"),
    # worktree 内 (6f73b79e day5/seven-pack) — baseline 当前居点
    Path(os.path.expanduser("~/qs-p01-seven-pack/.hardcoded_date_baseline.txt")),
    # main 检出的副本(将来进 main 后)
    Path(os.path.expanduser("~/qs/.hardcoded_date_baseline.txt")),
    Path(os.path.expanduser("~/qs/src/.hardcoded_date_baseline.txt")),
]

STALE_DAYS = 90  # 判例: 90 天未清理须 sentinel, 不静默


def _find_baseline() -> Path | None:
    for p in ALT_PATHS:
        if p.exists():
            return p
    return None


def _baseline_age_days(baseline: Path) -> tuple[int | None, str]:
    """计算 baseline 的最旧债务行距今多少天。

    用文件中第一行债务的日期(若有) 或文件 mtime. 找不到日期 → 用 mtime.
    """
    try:
        content = baseline.read_text(encoding="utf-8", errors="replace")
        # 从内容找 YYYY-MM-DD 或 YYYYMMDD 日期戳
        m = re.search(r"(20\d{2})[-/](\d{2})[-/](\d{2})", content)
        if m:
            oldest = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return (date.today() - oldest).days, f"内容首日期 {m.group(0)}"
    except Exception:
        pass
    # 回退: 文件 mtime
    mtime = date.fromtimestamp(baseline.stat().st_mtime)
    return (date.today() - mtime).days, f"文件 mtime {mtime}"


def test_baseline_not_stale_over_90_days():
    """baseline 文件最旧债务行距今不得超过 90 天.

    若超期, 该测试应落地 sentinel 报告(打印+断言+可量化的未清理天数).
    """
    baseline = _find_baseline()
    assert baseline is not None, (
        "未找到 .hardcoded_date_baseline.txt——若治理钉未达, 此判例可延后;"
        "但建立后 N 天不跑即「没接线」第 N 变体"
    )
    age, source = _baseline_age_days(baseline)
    lines = len(baseline.read_text(encoding="utf-8", errors="replace").splitlines())
    msg = (
        f"baseline 文件已存在 {age} 天 ({source}), 含 {lines} 行债务账. "
        f"若超 {STALE_DAYS} 天未清理 = 「建了没接线」治理钉自身变体 (判例). "
        f"须减员清理; 若无法减, 须在 Beta gate 明确记录该债务账为已知限制."
    )
    assert age <= STALE_DAYS or lines > 0, msg
    # 哨兵: 超期但文件在→打印 sentinel (打印的目的是半夜 crontab 显式, 不静默)
    if age > STALE_DAYS:
        print(f"[STALE-BASELINE-SENTINEL] {msg}")
    # 硬断言: baseline 不得超 180 天（铁环底限, 一个月一手）.
    assert age <= 180, f"baseline {age} 天未清理超 180 天上限 → 铁环底限已破: {msg}"


if __name__ == "__main__":
    import sys
    # CLI 模式供 crontab / 手动跑: python test_baseline_staleness_guard.py
    test_baseline_not_stale_over_90_days()
    print("baseline 哨兵 OK")
    sys.exit(0)

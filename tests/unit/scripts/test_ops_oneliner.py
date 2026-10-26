# -*- coding: utf-8 -*-
"""ops_oneliner 数据可信度加固 (TASK_20260921_01 定谳: 保留运维一行, 措辞向「数据可信度」调档) — 红绿测试。

断言:
1. 健康值中文化: green→绿色 / yellow→黄色 / red→红色 / 其他→未知, 不裸漏英文状态。
2. 中文色后跟人话影响提示 (green=依据完整 / yellow=信心关注 / red=信心打折)。
3. 运行计数与未决告警数保持原位不变。
"""
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "scripts" / "marimo_pages"))

import _common as C  # noqa: E402


def _render(health, runs=(10, 9, 1, 0), open_alerts=3):
    calls = iter([runs, (health,), (open_alerts,)])
    with patch.object(C, "data_asof", return_value="2026-09-21"), \
         patch.object(C, "one", side_effect=lambda *a, **k: next(calls)):
        return C.ops_oneliner()


class TestOpsOnelinerHealthCn:
    def test_red_chinese_with_hint(self):
        line = _render("red")
        assert "数据可信度 红色" in line
        assert "信心打折" in line
        assert "red" not in line

    def test_yellow_chinese_with_hint(self):
        line = _render("yellow")
        assert "数据可信度 黄色" in line
        assert "信心关注" in line
        assert "yellow" not in line

    def test_green_chinese_with_hint(self):
        line = _render("green")
        assert "数据可信度 绿色" in line
        assert "依据完整" in line
        assert "green" not in line

    def test_unknown_health_fallback(self):
        line = _render("degraded")
        assert "数据可信度 未知" in line
        assert "degraded" not in line

    def test_counts_and_alerts_preserved(self):
        line = _render("red", runs=(12, 10, 2, 0), open_alerts=7)
        assert "运行 12（✅10 ❌2 ⏭0）" in line
        assert "未决告警 7" in line
        assert "2026-09-21" in line

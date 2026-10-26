"""DL-65: cron_watchdog 预算公式需覆盖带依赖管道的等待窗。

门禁: quality_check(240s)/freshness_audit(240s)/paper_trading(420s)
带 depends_on → 预算 >= 900(等待窗) + grace。
此前预算=timeout_s+grace(360s/540s) < 900s → 上游延迟时 watchdog
在 wait_for_dependencies 期间 SIGTERM, 进程死于落记录前。
"""
import subprocess
import tempfile
import os
import pytest
import sys

QS_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
WATCHDOG = os.path.join(QS_ROOT, "scripts", "cron_watchdog.sh")
PY = sys.executable  # 用当前解释器 — worktree 无 .venv, 硬构造路径必炸 (worktree env-trap 族)


def _extract_budget(key: str, yaml_content: str) -> tuple[int, int]:
    """从 cron_watchdog.sh 提取预算计算逻辑并执行。"""
    # 提取 heredoc 中的 Python 代码
    with open(WATCHDOG, encoding="utf-8") as f:
        content = f.read()
    # 找 PYEOF 之间的 Python 代码
    start = content.index("<<'PYEOF'") + len("<<'PYEOF'")
    end = content.index("PYEOF", start)
    py_code = content[start:end].strip()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False, encoding='utf-8') as f:
        f.write(yaml_content)
        yaml_path = f.name
    try:
        r = subprocess.run(
            [PY, "-c", f"""
import sys
sys.argv = ['x', '{key}', '{yaml_path}']
{py_code}
"""],
            capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, f"budget calc failed: {r.stderr}"
        parts = r.stdout.strip().split()
        return int(parts[0]), int(parts[1])
    finally:
        os.unlink(yaml_path)


YAML_WITH_DEPS = """
ops:
  cron_watchdog:
    grace_s: 120
    default_timeout_s: 300
  pipelines:
    quality_check:
      timeout_s: 240
      depends_on: [daily_data_pipeline]
    freshness_audit:
      timeout_s: 240
      depends_on: [daily_data_pipeline]
    paper_trading:
      timeout_s: 420
      depends_on: [factor_update]
    factor_update:
      timeout_s: 1800
      depends_on: [daily_data_pipeline]
    daily_backtest:
      timeout_s: 600
"""


class TestDL65Budget:
    def test_quality_check_budget_covers_wait_window(self):
        total, grace = _extract_budget("quality_check", YAML_WITH_DEPS)
        # 预算必须 >= 900(等待窗) + grace
        assert total >= 900 + grace, f"quality_check budget {total}s < 900+{grace}s"

    def test_freshness_audit_budget_covers_wait_window(self):
        total, grace = _extract_budget("freshness_audit", YAML_WITH_DEPS)
        assert total >= 900 + grace, f"freshness_audit budget {total}s < 900+{grace}s"

    def test_paper_trading_budget_covers_wait_window(self):
        total, grace = _extract_budget("paper_trading", YAML_WITH_DEPS)
        assert total >= 900 + grace, f"paper_trading budget {total}s < 900+{grace}s"

    def test_factor_update_unchanged_when_already_large(self):
        """factor_update timeout_s=1800 > 900+margin, 预算不变。"""
        total, grace = _extract_budget("factor_update", YAML_WITH_DEPS)
        assert total == 1800 + grace

    def test_no_deps_pipeline_unchanged(self):
        """无依赖管道预算=timeout_s+grace, 不受影响。"""
        total, grace = _extract_budget("daily_backtest", YAML_WITH_DEPS)
        assert total == 600 + grace

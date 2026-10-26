"""OPS-03: 部署门禁 — 防冲突标记/语法错误进生产

08-20 事故: bootstrap.py 残留 git 冲突标记 (7个尖括号+HEAD), 全部 cron 管道
SyntaxError 崩溃。合并→生产之间零验证环节是流程病灶; 本脚本作为 crontab
管道命令的前置门禁:

    python scripts/deploy_gate.py && <原管道命令>

三项检查 (任一失败 exit 1):
  1. python -m compileall src/ -q          — 语法可编译
  2. 全仓冲突标记扫描 (7个< 或 7个> 开头)   — 排除 .git/tests 等
  3. python -c "import bootstrap" smoke    — 启动入口可加载 (PYTHONPATH=src)

fail-safe: 门禁失败走 AlertManager critical 告警, 但告警失败不掩盖门禁结果。
旁路: 环境变量 QS_DEPLOY_GATE_SKIP=1 (紧急时人工放行, 日志留痕)。

用法:
  PYTHONPATH=src .venv/bin/python scripts/deploy_gate.py
"""
import os
import subprocess
import sys

# 冲突标记扫描跳过的目录 (tests 内 fixture 可能合法包含标记字面量)
_SKIP_DIRS = {
    ".git", ".venv", "venv", "tests", "data", "logs", "reports",
    "__pycache__", "node_modules", ".pytest_cache", "backups",
}
# 只扫文本类扩展名
_SCAN_EXTS = {
    ".py", ".yaml", ".yml", ".md", ".sh", ".toml", ".cfg", ".ini", ".txt", ".json",
}
# 程序构造标记字面量, 避免本脚本自身被扫描命中
_MARKERS = ("<" * 7, ">" * 7)


def check_compileall(root: str, python: str) -> tuple[bool, str]:
    """检查 1: src/ 全部可编译。"""
    src = os.path.join(root, "src")
    if not os.path.isdir(src):
        return False, f"src/ 目录不存在: {src}"
    proc = subprocess.run(
        [python, "-m", "compileall", "-q", src],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip()[-2000:]
        return False, f"compileall 失败:\n{detail}"
    return True, "compileall ok"


def check_conflict_markers(root: str) -> tuple[bool, str]:
    """检查 2: 全仓无 git 冲突标记残留。"""
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in _SCAN_EXTS:
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        if any(m in line for m in _MARKERS):
                            hits.append(f"{os.path.relpath(path, root)}:{lineno}")
                            if len(hits) >= 20:
                                return False, "冲突标记残留 (截断):\n" + "\n".join(hits)
            except OSError:
                continue
    if hits:
        return False, "冲突标记残留:\n" + "\n".join(hits)
    return True, "no conflict markers"


def check_bootstrap_import(root: str, python: str) -> tuple[bool, str]:
    """检查 3: bootstrap 可加载 (cron 管道的公共入口)。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(root, "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [python, "-c", "import bootstrap"],
        capture_output=True, text=True, timeout=120, env=env, cwd=root,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip()[-2000:]
        return False, f"import bootstrap 失败:\n{detail}"
    return True, "import bootstrap ok"


def _send_gate_alert(root: str, failures: list[str]) -> None:
    """门禁失败告警 (fail-safe: 告警自身失败只打印, 不掩盖门禁结果)。"""
    try:
        sys.path.insert(0, os.path.join(root, "src"))
        from common.database import Database
        from ops_layer.alert_manager import AlertManager
        from ops_layer.core.models import Alert
        from ops_layer.core.tables import OPS_TABLES_DDL

        db = Database()
        conn = db.get_connection()
        conn.executescript(OPS_TABLES_DDL)
        conn.commit()
        AlertManager(db).send(Alert(
            severity="critical", layer="ops",
            title="部署门禁拦截: 代码不可部署",
            detail="\n---\n".join(failures)[:2000],
            action="修复后重跑 scripts/deploy_gate.py; 紧急情况 QS_DEPLOY_GATE_SKIP=1 旁路",
        ))
    except Exception as e:
        print(f"⚠️ 门禁告警发送失败 (不掩盖门禁结果): {e}", file=sys.stderr)


def run_gate(root: str | None = None, python: str | None = None,
             alert: bool = True) -> int:
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python = python or sys.executable

    if os.environ.get("QS_DEPLOY_GATE_SKIP") == "1":
        print("⚠️ QS_DEPLOY_GATE_SKIP=1 — 门禁旁路 (紧急放行, 请事后补验)")
        return 0

    checks = [
        ("compileall", lambda: check_compileall(root, python)),
        ("conflict_markers", lambda: check_conflict_markers(root)),
        ("bootstrap_import", lambda: check_bootstrap_import(root, python)),
    ]
    failures = []
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{name} 检查异常: {e}"
        icon = "✅" if ok else "❌"
        print(f"{icon} {name}: {detail.splitlines()[0] if detail else ''}")
        if not ok:
            failures.append(f"[{name}] {detail}")

    if failures:
        print(f"\n🚫 部署门禁未通过 ({len(failures)}/{len(checks)} 项失败) — 管道命令不应执行")
        if alert:
            _send_gate_alert(root, failures)
        return 1
    print(f"\n✅ 部署门禁通过 ({len(checks)}/{len(checks)})")
    return 0


if __name__ == "__main__":
    sys.exit(run_gate())

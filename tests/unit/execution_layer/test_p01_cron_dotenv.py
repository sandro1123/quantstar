"""P0-1 回归测试: cron 直驱入口必须自带 load_dotenv (CF-12: 从 crontab 枚举)。

CF-01 血案: 不经 src/__main__.py 启动的入口拿不到 bootstrap 的 load_dotenv;
CONFIG_ENV=prod 时 Config().load() 的必填校验硬抛 RuntimeError,
2026-08-21 单日崩 55+3 次。

CF-12 改版: 入口清单不再硬编码, 从 deploy/quantstar-crontab 枚举 ——
凡出现在线上 crontab、且不路由到 src/__main__ 的入口 (直驱 .py 文件,
或 `-m` 指到子模块), 都必须自带 load_dotenv。以后往 crontab 加新的
直驱入口, 本测试自动覆盖, 无需改本文件。
前提工件: deploy/quantstar-crontab (233583d 落库的线上快照)。

分层: 本文件的枚举层做「广度」(全部直驱入口, 文本断言);
intraday_scan / daily_update 两个历史血案入口另有「深度」运行时测试。
"""
import ast
import importlib
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CRONTAB = REPO_ROOT / "deploy" / "quantstar-crontab"
PROJECT_CD = "cd ~/qs"

# 历史血案入口必须始终被枚举到 (锚定解析器没有静默烂掉)
_MUST_INCLUDE_MODULES = (
    "src.execution_layer.scripts.intraday_scan",
    "src.execution_layer.scripts.daily_update",
)


def _parse_crontab_entries():
    """从 deploy/quantstar-crontab 枚举直驱入口。

    返回 set[(kind, id)]:
      ("file", "scripts/build_morning_brief.py")  — python 直接跑 .py 文件
      ("module", "src.xxx.yyy")                   — `python -m 子模块` (绕过 src/__main__)
    """
    entries = set()
    for raw in CRONTAB.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not ("/qs" in line or "~/qs" in line or "/quantstar" in line):  # 只认本项目任务行 (排除 token_watchdog 等外族行)
            continue
        m = re.search(r"/python[0-9.]*\s+([^>]+)", line)
        if not m:
            continue
        tokens = m.group(1).split()
        # 跳过解释器旗标 (-u 等); -m 不是旗标, 是路由信息, 要保留
        i = 0
        while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-m":
            i += 1
        if i >= len(tokens):
            continue
        if tokens[i] == "-m":
            if i + 1 >= len(tokens):
                continue
            module = tokens[i + 1]
            if module == "src":
                continue  # 经 src/__main__ → bootstrap_app 已带 load_dotenv
            entries.add(("module", module))
        elif tokens[i].endswith(".py"):
            path = Path(tokens[i])
            if path.is_absolute():
                try:
                    rel = path.resolve().relative_to(REPO_ROOT)
                except ValueError:
                    continue  # 项目外的 .py 不归本守卫管
            else:
                rel = path
            entries.add(("file", rel.as_posix()))
    return entries


def _entry_path(kind, ident):
    if kind == "file":
        return REPO_ROOT / ident
    return REPO_ROOT / (ident.replace(".", "/") + ".py")


_ENUMERATED = sorted(_parse_crontab_entries())


class TestCrontabDirectEntries:
    def test_crontab_artifact_exists(self):
        assert CRONTAB.is_file(), (
            "deploy/quantstar-crontab 缺失 — 守卫的前提工件没了 (落库于 233583d)"
        )

    def test_entries_nonempty_and_anchored(self):
        assert len(_ENUMERATED) >= 4, (
            f"枚举到的直驱入口过少 ({len(_ENUMERATED)}): {_ENUMERATED}"
            " — crontab 格式或解析器可能变了, 先人工核对再信本文件其余结论"
        )
        for module in _MUST_INCLUDE_MODULES:
            assert ("module", module) in _ENUMERATED, (
                f"历史血案入口 {module} 未被枚举到 — crontab 行或解析器变了,"
                " 先人工核对 (这两入口是 CF-01 的当事者, 不允许脱离守卫)"
            )


_ENTRY_IDS = [f"{kind}:{ident}" for kind, ident in _ENUMERATED]


def _calls_load_dotenv(path: Path) -> bool:
    """AST 层判定: 文件里是否真有 load_dotenv(...) 调用。

    用 AST 而非子串匹配 — 注释掉的调用 (含 NEG-CTRL 变异) 不得骗过本守卫。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # from dotenv import load_dotenv; load_dotenv(...)
        if isinstance(node.func, ast.Name) and node.func.id == "load_dotenv":
            return True
        # import dotenv; dotenv.load_dotenv(...)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "load_dotenv":
            return True
        # 兼容 from dotenv import load_dotenv as ld 的别名用法
        if isinstance(node, (ast.ImportFrom,)) and node.module == "dotenv":
            for alias in node.names:
                if alias.name == "load_dotenv" and alias.asname:
                    alias_name = alias.asname
                    for n2 in ast.walk(tree):
                        if (
                            isinstance(n2, ast.Call)
                            and isinstance(n2.func, ast.Name)
                            and n2.func.id == alias_name
                        ):
                            return True
    return False


@pytest.mark.parametrize("kind,ident", _ENUMERATED, ids=_ENTRY_IDS)
def test_direct_entry_loads_dotenv(kind, ident):
    path = _entry_path(kind, ident)
    assert path.is_file(), f"直驱入口指向的文件不存在: {path}"
    assert _calls_load_dotenv(path), (
        f"直驱入口 {ident} ({kind}) 缺真实的 load_dotenv 调用 — CONFIG_ENV=prod"
        " 下会复现 CF-01 (Config().load() 必填校验硬抛)。修法: 入口处"
        " load_dotenv(<repo>/.env)。注释掉的不算。"
    )


# ─── 深度层: 历史血案入口的运行时验证 (保留原 P0-1 测试) ─────────────

SECRET_KEYS = [
    "FUYAO_API_KEY", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
    "SENSENOVA_KEY1", "SENSENOVA_KEY2", "SENSENOVA_KEY3", "SENSENOVA_KEY4",
]


@pytest.fixture
def bare_prod_env(monkeypatch, tmp_path):
    """用临时凭据模拟 cron；不读取开发者或生产环境的 .env。"""
    import dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{key}=fixture-only-{key.lower()}" for key in SECRET_KEYS),
        encoding="utf-8",
    )
    real_load_dotenv = dotenv.load_dotenv

    def load_test_env(path, *args, **kwargs):
        assert Path(path).resolve() == REPO_ROOT / ".env"
        return real_load_dotenv(env_file, *args, **kwargs)

    for k in SECRET_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CONFIG_ENV", "prod")
    monkeypatch.setattr(dotenv, "load_dotenv", load_test_env)
    yield


def _fresh_import(module_name):
    """强制重新执行模块级代码 (含修复后的 load_dotenv)。"""
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


def test_intraday_scan_loads_dotenv_before_config(bare_prod_env):
    """导入脚本后秘密键应已来自 .env, Config().load() 不抛 CF-01。"""
    _fresh_import("execution_layer.scripts.intraday_scan")
    assert os.environ.get("FUYAO_API_KEY"), (
        "intraday_scan 导入后仍未加载 .env — cron 直驱入口缺 load_dotenv (P0-1)"
    )
    from config_layer import Config

    cfg = Config()
    cfg.load()  # 不得 raise RuntimeError [CF-01]
    assert cfg._loaded


def test_daily_update_loads_dotenv_before_config(bare_prod_env):
    """同上, daily_update 入口。"""
    _fresh_import("execution_layer.scripts.daily_update")
    assert os.environ.get("FUYAO_API_KEY"), (
        "daily_update 导入后仍未加载 .env — cron 直驱入口缺 load_dotenv (P0-1)"
    )
    from config_layer import Config

    cfg = Config()
    cfg.load()
    assert cfg._loaded

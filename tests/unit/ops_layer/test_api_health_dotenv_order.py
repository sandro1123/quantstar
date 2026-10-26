"""刀4顺手件: api_health_check.py 模块级 env 读取必须在 load_dotenv 之后。

缺陷形态: L31 API_TOKEN = os.environ.get("QS_API_TOKEN", "") 在模块级执行,
而 load_dotenv 在 _push_feishu 函数体内 (L82) — cron 裸跑时 .env 未加载,
API_TOKEN 恒空 → API 请求无 Authorization 头 → 可能 401。
修法: load_dotenv 提到模块级读取之前。
"""
import ast
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET = REPO_ROOT / "scripts" / "api_health_check.py"


def test_load_dotenv_before_module_env_read():
    """load_dotenv 调用必须出现在模块级 os.environ.get 之前 (按行号)。"""
    src = TARGET.read_text(encoding="utf-8")
    tree = ast.parse(src)

    load_dotenv_line = None
    first_env_read_line = None

    for node in ast.walk(tree):
        # 找 load_dotenv(...) 调用
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "load_dotenv":
                if load_dotenv_line is None or node.lineno < load_dotenv_line:
                    load_dotenv_line = node.lineno
        # 找模块级 os.environ.get / os.getenv
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("get", "getenv"):
                # 确认是 os.environ.get 或 os.getenv
                val = node.func.value
                if isinstance(val, ast.Attribute) and val.attr == "environ":
                    if first_env_read_line is None or node.lineno < first_env_read_line:
                        first_env_read_line = node.lineno
                elif isinstance(val, ast.Name) and val.id == "os" and node.func.attr == "getenv":
                    if first_env_read_line is None or node.lineno < first_env_read_line:
                        first_env_read_line = node.lineno

    assert load_dotenv_line is not None, "load_dotenv 调用未找到"
    assert first_env_read_line is not None, "模块级 env 读取未找到"
    assert load_dotenv_line < first_env_read_line, (
        f"load_dotenv (L{load_dotenv_line}) 必须在首个模块级 env 读取 "
        f"(L{first_env_read_line}) 之前"
    )

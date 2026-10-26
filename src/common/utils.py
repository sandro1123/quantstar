"""common/utils.py — 工具函数"""

import os
import re
import time
from datetime import datetime, timedelta, timezone

BJ_TZ = timezone(timedelta(hours=8))


def generate_trace_id() -> str:
    raw = f"{os.urandom(4).hex()}{int(time.time() * 1000) % 0x100000000:08x}"
    return raw[:12]


def bj_now() -> str:
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")


def bj_date() -> str:
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d")


def from_bj_str(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ_TZ)


_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_identifier(name: str, kind: str = "identifier") -> bool:
    """验证 SQL 标识符（表名、列名）是否合法，防止 SQL 注入"""
    if not name or not isinstance(name, str):
        return False
    return bool(_IDENTIFIER_RE.match(name))

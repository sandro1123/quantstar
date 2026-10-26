"""ops_layer/version_tracker.py — 版本追踪"""

import hashlib
import os
import subprocess

from common.database import Database
from common.logging import get_logger
from common.utils import bj_now
from ops_layer.core.models import VersionInfo

logger = get_logger("ops.version_tracker")


def git_head() -> tuple[str, str]:
    """返回 (short_commit, branch); git 不可用时返回 ('unknown', 'unknown')。"""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()
    except Exception:
        commit, branch = "unknown", "unknown"
    return commit, branch


def compute_config_hash() -> str:
    """config/ 下所有 yaml 的 sha256 指纹 (前 16 位)。"""
    config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "config")
    config_dir = os.path.abspath(config_dir)
    hasher = hashlib.sha256()
    if os.path.isdir(config_dir):
        for root, dirs, files in os.walk(config_dir):
            for f in sorted(files):
                if f.endswith((".yaml", ".yml")):
                    path = os.path.join(root, f)
                    rel = os.path.relpath(path, config_dir)
                    try:
                        with open(path, "rb") as fh:
                            hasher.update(rel.encode())
                            hasher.update(fh.read())
                    except OSError:
                        pass
    return hasher.hexdigest()[:16]



# ---------------------------------------------------------------------------
# 卡V: pipeline_runs 生效配置快照 (config_hash_effective) — 2026-08-30
#
# 与既有 compute_config_hash (文件面: config/ 目录 yaml 字节指纹) 的分工:
#   文件面: 文件改了生效值没变也会漂移 → 适合 version_history / P0-7 漂移哨兵;
#   生效面: registry 生效值 to_dict(show_secrets=False) 规范化 sha256 —
#           文件重排/注释/未消费键不误报, 派单语义「生效 yaml 规范化后」。
# 独立新列 config_hash_effective, 不改既有 compute_config_hash 语义
# (OPS-06 去重与 P0-7 都消费它)。老行天然 NULL, 不回填假值。
# ---------------------------------------------------------------------------

def _normalize_dict(obj):
    """规范化: 嵌套 dict 按键排序后序列化 — 键序无关, 值敏感."""
    if isinstance(obj, dict):
        return "{" + ",".join(
            repr(k) + ":" + _normalize_dict(obj[k]) for k in sorted(obj)
        ) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_normalize_dict(v) for v in obj) + "]"
    return repr(obj)


def _effective_config_dict() -> dict:
    """读生效配置 (registry 导出, 秘密脱敏) — 不可用时抛给上层 fail-safe."""
    from config_layer import get_config

    config = get_config()
    if not config._loaded:
        config.load()
    return config.to_dict(show_secrets=False)


def compute_effective_config_hash() -> str:
    """生效配置指纹: registry 生效值 (秘密脱敏) 规范化 sha256, 完整 64 hex.

    fail-safe: registry 不可用返回 '' (不炸调用方 — 管道记录优先),
    WARNING 兜底 (铁律 #12)。
    """
    try:
        d = _effective_config_dict()
        return hashlib.sha256(_normalize_dict(d).encode()).hexdigest()
    except Exception as e:
        logger.warning(f"compute_effective_config_hash failed (degrade to ''): {e}")
        return ""


PIPELINE_RUNS_NEW_COLUMNS = {
    "config_hash_effective": "TEXT",
}


def _ensure_pipeline_runs_columns(conn) -> None:
    """DL-43: 管道内幂等补列 — 不单独迁移脚本.

    OPS_TABLES_DDL 是 CREATE IF NOT EXISTS, 对已存在的生产表不会加新列;
    这里在管道入口 (start_run) 检缺列 -> ALTER TABLE ADD COLUMN (幂等吞
    duplicate column)。老行自动 NULL — 宁可 NULL 不回填假值。
    """
    existing = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_runs)")}
    for col, typ in PIPELINE_RUNS_NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE pipeline_runs ADD COLUMN {col} {typ}")


class VersionTracker:
    def __init__(self, db: Database):
        self.db = db

    def _compute_config_hash(self) -> str:
        return compute_config_hash()

    def record_version(self) -> VersionInfo:
        """记录当前版本; OPS-06 去重: 同 commit+config_hash 当日已有则跳过插入。

        bootstrap 每 5 分钟被 cron 触发, 不去重会刷爆 version_history。
        """
        commit, branch = git_head()
        config_hash = compute_config_hash()
        now = bj_now()
        today = now.split(" ")[0]

        info = VersionInfo(
            code_version=commit,
            code_branch=branch,
            config_hash=config_hash,
            data_version=config_hash,
            deployed_at=now,
        )

        conn = self.db.get_connection()
        # 去重: 同日同 commit+hash 已记录则不重复插入
        existing = conn.execute(
            "SELECT 1 FROM version_history WHERE code_version=? AND config_hash=? "
            "AND deployed_at LIKE ? LIMIT 1",
            (commit, config_hash, f"{today}%"),
        ).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO version_history (code_version, code_branch, config_hash, data_version, deployed_at)
                VALUES (?, ?, ?, ?, ?)
            """, (commit, branch, config_hash, config_hash, now))
            conn.commit()
        return info

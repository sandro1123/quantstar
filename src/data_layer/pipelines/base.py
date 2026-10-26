"""pipelines/base.py — 管道基类 + 契约"""
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from common.database import Database


@dataclass
class PipelineInput:
    date: str
    start_date: str | None = None
    end_date: str | None = None
    mode: str = "daily"
    symbols: list[str] | None = None
    dry_run: bool = False
    config: dict = field(default_factory=dict)
    db: "Database | None" = None

    @property
    def effective_start(self) -> str:
        return self.start_date or self.date

    @property
    def effective_end(self) -> str:
        return self.end_date or self.date


@dataclass
class PipelineResult:
    name: str
    date: str
    status: str
    records_read: int = 0
    records_written: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.status in ("success", "partial")


# ---------------------------------------------------------------------------
# DL-32: dry_run 基类统一拦截
# ---------------------------------------------------------------------------
# 契约: dry_run=True 时业务逻辑照跑, 但对数据库的 **数据写入 (DML)** 一律被基类
# 丢弃, 目标表行数不变; 读取 (SELECT/PRAGMA) 与幂等 DDL (CREATE/ALTER) 透传,
# 保证依赖"建表后读取"或"增量覆盖判断"的管线逻辑能正常走完。
#
# 为什么在基类做: 17 个管线里 16 个不检查 PipelineInput.dry_run (事故簿 事故 05:
# 统筹以 dry_run=True 跑 lhb_daily 仍真写生产库 54 行)。契约字段若只靠子类自觉,
# 则视为不存在 —— 故在唯一入口 execute() 强制拦截, 并由测试逐个覆盖 17 管线。
#
# 拦截方式 (回执说明 a vs b): 选 a —— 连接级只读代理。读透传、写丢弃并计数。
# 相比 b(未适配子类 fail-closed 拒绝执行), a 能让业务逻辑真跑一遍 (验证增量/覆盖
# 分支), 同时用"丢弃计数 > 0"证明拦截生效; 测试再对 17 管线逐个断言行数不变,
# 兼得 a 的可用性与 b 的安全性。
#
# 可观测 (铁律 #12): 被拦截的写操作计数写入 result.metadata["dry_run_writes_blocked"],
# 不静默。

# 透传 (到达真实连接) 的 SQL 首关键字: 读取 + 幂等 DDL。
# 其余 (INSERT/UPDATE/DELETE/REPLACE/BEGIN/COMMIT/ROLLBACK/DROP/...) 一律丢弃。
_PASSTHROUGH_PREFIXES = (
    "SELECT", "PRAGMA", "WITH", "EXPLAIN", "CREATE", "ALTER",
)


def _is_passthrough(sql: str) -> bool:
    head = str(sql or "").lstrip().upper()
    return head.startswith(_PASSTHROUGH_PREFIXES)


class _DryRunCursor:
    """被丢弃写操作的占位游标 — 不触达真实连接。"""

    rowcount = 0
    lastrowid = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size=None):
        return []

    def __iter__(self):
        return iter(())


class _DryRunConnection:
    """包裹真实 sqlite3.Connection: 读/幂等 DDL 透传, DML 与事务控制丢弃并计数。"""

    def __init__(self, real_conn, counter: list):
        self._conn = real_conn
        self._counter = counter  # 单元素列表, 跨 proxy 共享计数

    def execute(self, sql, params=None):
        if _is_passthrough(sql):
            if params is None:
                return self._conn.execute(sql)
            return self._conn.execute(sql, params)
        self._counter[0] += 1
        return _DryRunCursor()

    def executemany(self, sql, seq):
        # 整批视为被拦截的数据写入
        self._counter[0] += 1
        return _DryRunCursor()

    def executescript(self, script):
        # 现网仅用于 _SCHEMA 纯 DDL 建表脚本 → 透传 (幂等, 不产生行)。
        return self._conn.executescript(script)

    def commit(self):
        # 无真实写入, 提交为 no-op (不触达真实连接, 避免误提交他人事务)。
        return None

    def rollback(self):
        return None

    def cursor(self):
        return _DryRunCursor()

    def __getattr__(self, name):
        # row_factory / close 等其余属性透传到真实连接。
        return getattr(self._conn, name)


class _DryRunDatabase:
    """包裹 Database: get_connection() 返回只读代理连接。"""

    def __init__(self, real_db, counter: list):
        object.__setattr__(self, "_db", real_db)
        object.__setattr__(self, "_counter", counter)
        object.__setattr__(self, "_proxy_conn", None)

    def get_connection(self):
        if object.__getattribute__(self, "_proxy_conn") is None:
            real_conn = self._db.get_connection()
            object.__setattr__(
                self, "_proxy_conn", _DryRunConnection(real_conn, self._counter))
        return object.__getattribute__(self, "_proxy_conn")

    # Database 也暴露 execute/executemany/commit — 一律走代理连接, 防止绕过拦截。
    def execute(self, sql, params=None):
        if params is None:
            return self.get_connection().execute(sql)
        return self.get_connection().execute(sql, params)

    def executemany(self, sql, seq):
        return self.get_connection().executemany(sql, seq)

    def commit(self):
        return self.get_connection().commit()

    def __getattr__(self, name):
        # db_path / close / init_db 等透传到真实 Database。
        return getattr(object.__getattribute__(self, "_db"), name)


class BasePipeline(ABC):
    name: str = ""
    requires: list[str] = []
    produces: list[str] = []

    @abstractmethod
    def run(self, input: PipelineInput) -> PipelineResult:
        ...

    def execute(self, input: PipelineInput) -> PipelineResult:
        if input.dry_run:
            return self._execute_dry_run(input)
        return self._execute_real(input)

    def _execute_real(self, input: PipelineInput) -> PipelineResult:
        start = time.time()
        try:
            result = self.run(input)
            result.duration_s = time.time() - start
            return result
        except Exception as e:
            # DL-03 (D91 事务性写入): 异常时回滚未提交写入, 不留半批数据。
            # 已在 run() 内 commit 的批次不受影响; 此处兜底未 commit 的部分。
            db = input.db if input.db is not None else getattr(self, "db", None)
            if db is not None and hasattr(db, "get_connection"):
                try:
                    db.get_connection().rollback()
                except Exception:
                    pass
            return PipelineResult(
                name=self.name, date=input.date,
                status="failed", errors=[str(e)],
                duration_s=time.time() - start,
            )

    def _execute_dry_run(self, input: PipelineInput) -> PipelineResult:
        counter = [0]
        orig_input_db = input.db
        had_self_db = hasattr(self, "db")
        orig_self_db = getattr(self, "db", None)

        # get_repo() 有模块级单例缓存 (repos._repo_instance)。dry_run 期间若以代理 db
        # 重建过该缓存, 结束后必须还原, 否则后续生产调用 get_repo(None) 会拿到仍被拦截
        # 的旧容器 (代理泄漏)。
        try:
            from data_layer import repos as _repos_mod
            orig_repo_instance = _repos_mod._repo_instance
        except Exception:
            _repos_mod = None
            orig_repo_instance = None

        # 兜底真实库: 两者皆空时预建一个 (解析默认路径), 供代理包裹,
        # 避免 run() 内 `if not input.db and not self.db: self.db = Database()` 自建连接绕过拦截。
        fallback_db = None
        if orig_input_db is None and orig_self_db is None:
            from common.database import Database
            fallback_db = Database()

        if orig_input_db is not None:
            input.db = _DryRunDatabase(orig_input_db, counter)
        elif fallback_db is not None:
            input.db = _DryRunDatabase(fallback_db, counter)

        # self.db 也必须拦截: news.py 直接把 self.db 传给 NewsCollector, 忽略 input.db。
        if orig_self_db is not None:
            self.db = _DryRunDatabase(orig_self_db, counter)
        else:
            base = orig_input_db if orig_input_db is not None else fallback_db
            if base is not None:
                self.db = _DryRunDatabase(base, counter)

        start = time.time()
        try:
            result = self.run(input)
            result.duration_s = time.time() - start
            result.metadata = dict(result.metadata or {})
            result.metadata["dry_run"] = True
            result.metadata["dry_run_writes_blocked"] = counter[0]
            return result
        except Exception as e:
            return PipelineResult(
                name=self.name, date=input.date,
                status="failed", errors=[str(e)],
                duration_s=time.time() - start,
                metadata={"dry_run": True, "dry_run_writes_blocked": counter[0]},
            )
        finally:
            # 还原, 防止代理泄漏到 execute() 之外。
            input.db = orig_input_db
            if had_self_db:
                self.db = orig_self_db
            else:
                try:
                    del self.db
                except AttributeError:
                    pass
            if _repos_mod is not None:
                try:
                    _repos_mod._repo_instance = orig_repo_instance
                except Exception:
                    pass

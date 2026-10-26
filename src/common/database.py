"""数据库管理 — 连接 + 初始化 + 迁移"""

import sqlite3
import threading


class _LockSafeConnection(sqlite3.Connection):
    """写语句失败 → 当场回滚释放语句级写事务 (锁窗卡 2026-08-30)。

    病灶: sqlite3 写语句撞 CHECK/约束失败时, 隐式 BEGIN 的写事务不自动
    回滚; 调用方 (如 _recover_fallback_records / @catch_quantstar_error
    装饰的方法) except 吞异常不清理, 连接挂着写锁回缓存 → 后续任何连接
    (含跨进程) 写同库全部 'database is locked', busy_timeout 等不到
    (持有者已死)。T3 现场重建实证 (tests/unit/common/test_write_lock_release.py)。

    修法: 覆盖 execute/executemany 两条语句入口, 失败时回滚**仅由该
    失败语句自己开启的事务**: 事务状态跃迁 (语句前无事务 + 失败后处于
    事务中) 精确识别之。SELECT 失败不会开启写事务, 不误触发; 调用方
    先 BEGIN 再多语句写的合法场景, 失败语句前已 in_transaction, 跃迁
    不成立, 待写内容不被丢弃, 语义与裸 sqlite3 一致。异常原样上抛。
    """

    def execute(self, sql, params=()):
        was_in_tx = self.in_transaction
        try:
            return super().execute(sql, params)
        except sqlite3.Error:
            if not was_in_tx and self.in_transaction:
                try:
                    self.rollback()
                except sqlite3.Error:
                    pass
            raise

    def executemany(self, sql, seq):
        was_in_tx = self.in_transaction
        try:
            return super().executemany(sql, seq)
        except sqlite3.Error:
            if not was_in_tx and self.in_transaction:
                try:
                    self.rollback()
                except sqlite3.Error:
                    pass
            raise


class Database:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            import os
            # P0-2: 测试隔离 — QS_TEST_DB 由 tests/conftest.py 设置, 指向会话级临时库。
            # 生产从不设置此变量, 默认路径解析不变 (data/qs.db 相对解析)。
            db_path = os.environ.get("QS_TEST_DB") or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "data", "qs.db")
        self.db_path = db_path
        self._conn = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    @staticmethod
    def _is_closed(conn: sqlite3.Connection) -> bool:
        """探测缓存连接是否已被调用方关闭。

        sqlite3 连接无公开的 is_closed API; 对已关闭连接访问 total_changes
        抛 ProgrammingError, 打开的连接返回 int (便宜、无副作用)。
        """
        try:
            conn.total_changes
            return False
        except sqlite3.ProgrammingError:
            return True

    def get_connection(self) -> sqlite3.Connection:
        # P0: 调用方可能对共享缓存连接直接 .close() (如 orchestrator
        # _run_factor_update/_run_daily_backtest 的 conn.close()), 缓存里
        # 留下已关闭对象, 后续取用抛 "Cannot operate on a closed database"。
        # 在此单点检测并重连, 覆盖全部 get_connection 调用点。
        if self._conn is None or self._is_closed(self._conn):
            with self._lock:
                if self._conn is None or self._is_closed(self._conn):
                    self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10, factory=_LockSafeConnection)
                    self._conn.row_factory = sqlite3.Row
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.execute("PRAGMA foreign_keys=ON")
                    self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def init_db(self):
        conn = self.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO schema_version (id, version)
            VALUES (1, 1)
        """)
        conn.commit()

    def execute(self, sql: str, params: tuple | None = None):
        conn = self.get_connection()
        with self._write_lock:
            if params:
                return conn.execute(sql, params)
            return conn.execute(sql)

    def executemany(self, sql: str, seq: list):
        conn = self.get_connection()
        with self._write_lock:
            return conn.executemany(sql, seq)

    def commit(self):
        conn = self.get_connection()
        with self._write_lock:
            conn.commit()

    def close(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

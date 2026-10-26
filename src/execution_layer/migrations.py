"""stock_pool schema 迁移 — 声明式迁移 + PRAGMA table_info 差集幂等补列

背景 (S4/F4.1):
    生产 stock_pool (53 列) 比 tables.py STOCK_POOL_DDL 权威 schema (65 列) 缺 12 列。
    `CREATE TABLE IF NOT EXISTS` 不升级旧表, 全仓此前无 migration 机制。

ensure_schema() 语义:
    1. 确保 schema_version 表存在 (与 common.database.Database.init_db 同构)
    2. stock_pool 已存在时, 用 PRAGMA table_info 差集补缺失列 (ALTER TABLE ADD COLUMN, 幂等)
    3. 非 ALTER 语句 (如 CREATE TABLE delivery_log) 无条件执行 (语句自身幂等)
    4. 落后于目标版本时向 schema_version 追加新版本行
    stock_pool 不存在时 (全新库) 跳过 ALTER 列补齐 — start() 会用完整 DDL 建表。

用法:
    from execution_layer.migrations import ensure_schema
    ensure_schema(db)   # db: common.database.Database
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from common.logging import get_logger

if TYPE_CHECKING:
    from common.database import Database

logger = get_logger("execution.migrations")

# AUD-001: memory_store schema 唯一权威定义点。
# 两条建表路径 (本迁移 v5 / memory_layer.store._ensure_mem_schema) 必须同源,
# 否则测试库与生产库 schema 语义分叉 (旧实现分叉三处: created_at 默认 UTC、
# 丢 NOT NULL、丢两索引) —— 测的不是生产形态, 属假绿族。
MEMORY_STORE_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS memory_store (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        evidence TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.5,
        created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
        expires_at TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        superseded_by INTEGER REFERENCES memory_store(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_store_scope ON memory_store (scope, status)",
    "CREATE INDEX IF NOT EXISTS idx_memory_store_created ON memory_store (created_at)",
)

# 声明式迁移清单: (version, [DDL 语句])
# v2: stock_pool 补齐 12 列 (对齐 tables.py STOCK_POOL_DDL 权威 schema)
# v3: delivery_log 推送日志表 (S5/F5.2c, DDL 与 delivery_layer/log_store.py 一致)
MIGRATIONS: list[tuple[int, list[str]]] = [
    (2, [
        "ALTER TABLE stock_pool ADD COLUMN atr_stop REAL",
        "ALTER TABLE stock_pool ADD COLUMN total_fees REAL DEFAULT 0",
        "ALTER TABLE stock_pool ADD COLUMN net_return REAL",
        "ALTER TABLE stock_pool ADD COLUMN net_return_pct REAL",
        "ALTER TABLE stock_pool ADD COLUMN add_position_count INTEGER DEFAULT 0",
        "ALTER TABLE stock_pool ADD COLUMN reduce_position_count INTEGER DEFAULT 0",
        "ALTER TABLE stock_pool ADD COLUMN partial_exit_count INTEGER DEFAULT 0",
        "ALTER TABLE stock_pool ADD COLUMN swap_count INTEGER DEFAULT 0",
        "ALTER TABLE stock_pool ADD COLUMN latest_adjustment_action TEXT",
        "ALTER TABLE stock_pool ADD COLUMN latest_adjustment_date TEXT",
        "ALTER TABLE stock_pool ADD COLUMN position_pct_initial REAL",
        "ALTER TABLE stock_pool ADD COLUMN avg_entry_price REAL",
    ]),
    (3, [
        """CREATE TABLE IF NOT EXISTS delivery_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_delivery_log_created ON delivery_log (created_at)",
    ]),
    (4, [
        "ALTER TABLE stock_pool ADD COLUMN exit_source TEXT",
    ]),
    # v5: memory_store — QS 第十层记忆系统 Phase 1 (F, 2026-08-28)。
    # 表名裁定: 既有 agent_memory 是预测缓存 (quality/repository.py), 记忆表另名。
    # 跨层表进中央清单先例 = v3 delivery_log。
    # AUD-001: v5 语句体已提为模块级 MEMORY_STORE_DDL (唯一权威定义点) —
    # memory_layer/store.py 的"全新库"路径 import 它, 不得另写一份。
    (5, list(MEMORY_STORE_DDL)),
    # v6: chart_analysis — 09 图形读图答题工人结论表 (#101 切1, F, 2026-09-03)。
    # 跨层表进中央清单先例 = v3 delivery_log / v5 memory_store; 表即跨层界面 (§1.5):
    # 唯一写者 = 09 chart_reader, 永不与 02 的 signal/pending_entry 相交。
    (6, [
        """CREATE TABLE IF NOT EXISTS chart_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            pattern TEXT,
            entry_low REAL,
            entry_high REAL,
            stop REAL,
            profit REAL,
            confidence REAL,
            reasons TEXT,
            source TEXT NOT NULL,
            levels_json TEXT,
            atr_baseline REAL,
            llm_raw TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
            UNIQUE (code, date, source)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_chart_analysis_date ON chart_analysis (date)",
        "CREATE INDEX IF NOT EXISTS idx_chart_analysis_code ON chart_analysis (code, date)",
    ]),

    # v7: report_section_trace — 报告段留痕 (B席 ⑤ 试运行条件⑤, 2026-09-16)。
    # 跨层表进中央清单先例 = v3 delivery_log / v5 memory_store / v6 chart_analysis。
    # 唯一写者 = delivery_layer/log_store.DeliveryLogStore.record_sections;
    # DDL 权威定义点在 log_store.SECTION_TRACE_DDL, 此处保持一致。
    (7, [
        """CREATE TABLE IF NOT EXISTS report_section_trace (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL,
            section_index INTEGER NOT NULL,
            section_name TEXT NOT NULL,
            char_count INTEGER NOT NULL DEFAULT 0,
            is_empty INTEGER NOT NULL DEFAULT 0,
            degraded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_rst_ct_date ON report_section_trace (content_type, date)",
    ]),
    # v8: 告警持续观测与升级节流；旧行保留 NULL，不伪造历史。
    (8, [
        "ALTER TABLE alerts ADD COLUMN last_escalated_at TEXT",
        "ALTER TABLE alerts ADD COLUMN last_seen TEXT",
    ]),
    # v9: advisor_guard_trace — 忠实性守卫降级留痕 (模块1b, 2026-09-21)。
    # 跨层表进中央清单先例 = v3/v5/v6/v7。唯一写者 = report_layer/advisor/guard_trace;
    # DDL 权威定义点在 guard_trace.GUARD_TRACE_DDL, 此处保持一致。
    # 只记拒绝不记放行 (留痕面=取证异常非流水账)。无既有守卫留痕表可复用 (PRAGMA 实证,
    # report_section_trace=段级留痕粒度不同)。
    (9, [
        """CREATE TABLE IF NOT EXISTS advisor_guard_trace (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            rejected_gate TEXT NOT NULL,
            degrade_path TEXT NOT NULL,
            llm_output_hash TEXT,
            detail TEXT NOT NULL DEFAULT '',
            rationale_id TEXT,
            trace_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agt_date ON advisor_guard_trace (date)",
        "CREATE INDEX IF NOT EXISTS idx_agt_code ON advisor_guard_trace (code, date)",
    ]),
    # v10: REC 持久化面通电 (通电卡, 2026-09-24, 用户裁定执行层+甲案)。
    # 病根: REC 四表代码全并、生产零通电 —— 1a/模块4 走懒建 (DDL 只在
    # record_rationale()/tracker 被调时 executescript), 而两处调用方生产为零
    # → 表永不建 (对照: 模块3 recommendation_retrospect 自己建表故有行)。
    # 光接线不够: 必须入中央清单, 否则首次调用与建表仍隐式耦合 (6f73b79e 精化)。
    #
    # 方案甲 (统筹 09-24 裁定): 字面量内联于此 + 对拍测试锁漂移
    # (test_v10_ddl_matches_authoritative 读三处权威常量逐字段比对, 漂移即 RED)。
    # 迁移保持零业务依赖 (不 import 业务模块, 免反向依赖)。
    #
    # 权威定义点 (改那里须同步此处):
    #   recommendation_tracking / recommendation_tracking_advice →
    #       execution_layer/tracking/recommendation_tracker.py (TRACKING_DDL/ADVICE_DDL)
    #   recommendation_rationale → agent_layer/rationale.py (RATIONALE_DDL, 契约冻结 fe45b0f3)
    # 增量三字段 (confidence_tier/confidence_reason/entry_basis_trace_id) 直接进
    # 建表语句 (新表无需 ALTER); state_transitions 不建 (统筹裁: basis_chain JSON 已承载)。
    (10, [
        """CREATE TABLE IF NOT EXISTS recommendation_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            rec_date TEXT NOT NULL,
            rationale_id TEXT DEFAULT '',
            verdict TEXT DEFAULT '',
            confidence REAL,
            state TEXT NOT NULL DEFAULT 'recommended',
            basis_chain TEXT DEFAULT '[]',
            confidence_tier TEXT DEFAULT '',
            confidence_reason TEXT DEFAULT '',
            entry_basis_trace_id TEXT DEFAULT '',
            started_at TEXT DEFAULT (datetime('now', '+8 hours')),
            updated_at TEXT DEFAULT (datetime('now', '+8 hours')),
            UNIQUE (code, rec_date)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_rec_tracking_code ON recommendation_tracking (code)",
        "CREATE INDEX IF NOT EXISTS idx_rec_tracking_state ON recommendation_tracking (state)",
        """CREATE TABLE IF NOT EXISTS recommendation_tracking_advice (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            advice_date TEXT NOT NULL,
            state TEXT DEFAULT '',
            advice TEXT NOT NULL,
            basis TEXT NOT NULL,
            evidence TEXT DEFAULT '',
            is_reversal INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', '+8 hours')),
            UNIQUE (code, advice_date)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_rec_advice_date ON recommendation_tracking_advice (advice_date)",
        """CREATE TABLE IF NOT EXISTS recommendation_rationale (
            rationale_id TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            rec_date TEXT NOT NULL,
            verdict TEXT,
            confidence REAL,
            position_pct REAL,
            reasoning TEXT,
            expert_name TEXT,
            key_points TEXT,
            invalidation_conditions TEXT,
            claims TEXT,
            trace_id TEXT,
            run_id TEXT,
            request_id TEXT,
            advisor_text TEXT,
            advisor_guard TEXT,
            created_at TEXT DEFAULT (datetime('now', '+8 hours')),
            UNIQUE (code, rec_date, expert_name)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_rec_rationale_code ON recommendation_rationale (code, rec_date)",
        "CREATE INDEX IF NOT EXISTS idx_rec_rationale_trace ON recommendation_rationale (trace_id)",
    ]),
    # v11: llm_token_usage — LLM 用量留痕 (留痕腿, 2026-09-25)。
    # 跨层表进中央清单先例 = v3/v5/v6/v7/v9。
    # 为什么必须 write-through 而非日终读内存: TokenBudget 是 ProviderManager 的
    # **进程内实例**, 而每条 cron pipeline 各起进程 -> 进程退出 _usage 即消失,
    # 「日终读 get_all_usage」根本行不通。唯一写者 = agent_layer/llm/usage_ledger;
    # DDL 权威定义点在 usage_ledger.USAGE_LEDGER_DDL, 此处保持一致 (列一次到位,
    # 免 v11 后 v12 加列 —— 统筹裁定)。
    (11, [
        """CREATE TABLE IF NOT EXISTS llm_token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            call_point TEXT NOT NULL,
            bucket TEXT NOT NULL DEFAULT '',
            tokens INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            pid INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ltu_date_bucket ON llm_token_usage (date, bucket)",
        "CREATE INDEX IF NOT EXISTS idx_ltu_call_point ON llm_token_usage (call_point, date)",
    ]),
    # v12: stock_pool.rr_ratio 列迁移 (判例⑤: 列迁移批了未入中央清单, 2026-09-28)。
    # 病根: stock_pool.rr_ratio 列存在 (schema 有) 但迁移清单里没有它的 DDL
    # (ALTER TABLE stock_pool ADD COLUMN rr_ratio) —— 列建了但没进迁移,
    # 下一个 REC 读写点必再冒红 (factor_update `no such column: rr_ratio` 09-22 红)。
    # 既有行不丢 (ALTER ADD COLUMN 默认 NULL, 不回填 —— 判例「不回填旧数据」)。
    (12, [
        "ALTER TABLE stock_pool ADD COLUMN rr_ratio REAL",
    ]),
]

TARGET_VERSION = max(v for v, _ in MIGRATIONS)

_ADD_COLUMN_RE = re.compile(r"ADD\s+COLUMN\s+(\w+)", re.IGNORECASE)


def _column_name(alter_sql: str) -> str | None:
    m = _ADD_COLUMN_RE.search(alter_sql)
    return m.group(1) if m else None


def get_current_version(conn) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return (row[0] if row else None) or 1


def ensure_schema(db: Database) -> dict:
    """幂等补齐 stock_pool 至目标 schema。

    Returns:
        {"version_before": int, "version_after": int, "applied": [已执行的 ALTER 语句]}
    """
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )
    """)
    conn.execute("INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 1)")
    version_before = get_current_version(conn)

    applied: list[str] = []
    for version, stmts in MIGRATIONS:
        for stmt in stmts:
            col = _column_name(stmt)
            # 其他层可能先推进全局版本；补列仍须按实际 schema 检查。
            if col is None and version <= version_before:
                continue
            if col is not None:
                # 按 ALTER 的实际目标表判定，不能用 stock_pool 的存在性
                # 跳过独立运维库中的 alerts 升级。
                table = stmt.split()[2]
                columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if not columns or col in columns:
                    continue
            conn.execute(stmt)
            applied.append(stmt)

    version_after = version_before
    if version_before < TARGET_VERSION:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (TARGET_VERSION,))
        version_after = TARGET_VERSION
    conn.commit()

    if applied:
        logger.info(
            f"stock_pool schema 迁移 v{version_before}→v{version_after}: 补 {len(applied)} 列"
        )
    return {
        "version_before": version_before,
        "version_after": version_after,
        "applied": applied,
    }

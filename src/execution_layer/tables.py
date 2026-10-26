"""Execution Layer 表定义"""

STOCK_POOL_DDL = """
CREATE TABLE IF NOT EXISTS stock_pool (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('watch', 'pending_entry', 'active', 'exit_pending', 'exited')),
    source TEXT CHECK (source IN ('strategy', 'manual', 'agent')),
    added_date TEXT NOT NULL,
    initial_score REAL,
    added_reason TEXT,
    latest_signal_date TEXT,
    account_id TEXT DEFAULT 'default',
    fund_type TEXT DEFAULT 'simulated' CHECK (fund_type IN ('simulated', 'real')),
    watch_reason TEXT,
    entry_low REAL,
    entry_high REAL,
    valid_from TEXT,
    valid_until TEXT,
    entry_plan_source TEXT,
    entry_plan_date TEXT,
    entry_price REAL,
    entry_date TEXT,
    entry_reason TEXT,
    position_qty INTEGER DEFAULT 0,
    position_pct REAL DEFAULT 0,
    stop_loss REAL,
    trailing_stop REAL,
    atr_stop REAL,
        rr_ratio REAL, -- REC 模块2 (2026-09-21): 风险收益比=(目标-买)/(买-止损)
    take_profit REAL,
    highest_price REAL,
    highest_date TEXT,
    dynamic_stop_loss REAL,
    dynamic_stop_reason TEXT,
    current_score REAL,
    last_score_date TEXT,
    unrealized_pnl REAL,
    unrealized_pnl_pct REAL,
    holding_days INTEGER DEFAULT 0,
    last_price REAL,
    last_price_date TEXT,
    exit_reason TEXT,
    exit_reason_detail TEXT,
    exit_source TEXT CHECK (exit_source IN ('cleanup', 'engine', 'manual', NULL)),
    exit_layer INTEGER,
    trigger_price REAL,
    trigger_date TEXT,
    exit_low REAL,
    exit_high REAL,
    exit_plan_date TEXT,
    exit_price REAL,
    exit_date TEXT,
    total_return REAL,
    total_return_pct REAL,
    holding_days_actual INTEGER,
    total_fees REAL DEFAULT 0,
    net_return REAL,
    net_return_pct REAL,
    add_position_count INTEGER DEFAULT 0,
    reduce_position_count INTEGER DEFAULT 0,
    partial_exit_count INTEGER DEFAULT 0,
    swap_count INTEGER DEFAULT 0,
    latest_adjustment_action TEXT,
    latest_adjustment_date TEXT,
    position_pct_initial REAL,
    review_note TEXT,
    review_quality TEXT CHECK (review_quality IN ('correct', 'neutral', 'wrong', NULL)),
    avg_entry_price REAL,
    updated_at TEXT DEFAULT (datetime('now', '+8 hours')),
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

ACTION_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS action_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN (
        'add_to_watch', 'remove_from_watch', 'set_entry_range', 'cancel_entry',
        'cancel_exit', 'entry', 'skip_entry', 'add_position', 'reduce_position',
        'swap_exit', 'partial_exit', 'exit', 'skip_exit', 'exit_check',
        'exit_tracking', 'exit_tracking_check', 'status_change', 'hold',
        'sell_signal', 'adjust_stop_loss', 'adjust_take_profit',
        'adjust_trailing_stop', 'adjust_entry_range', 'pnl_change',
        'manual_override', 'volatility_alert',
        -- AUD-028 (2026-09-14): 以下为补登记。白名单是 26 项时代的口径,
        -- 生产表其实根本没有 action_type 约束, 两者早已不是同一张表。
        -- 漏登记的后果不是报错而是: 按本 DDL 新建的库 (测试/回测/paper
        -- trading/灾难恢复重建) 上, 这些写入一律抛 IntegrityError。
        -- EX-4 建议引擎面 (src 有写点, 新建库上此前必炸):
        'advisory_state', 'pool_downgrade', 'pool_exit',
        -- 生产在册的历史类型 (当前 src 已无写点, 但共 82 行在库):
        -- 按本 DDL 重建 action_records 再回灌生产数据时, 少一项就整批拒收。
        'buy_pending_review', 'sell_pending_review', 'buy_rejected',
        'stale_pending_cleanup'
    )),
    action_detail TEXT,
    price REAL,
    previous_value REAL,
    new_value REAL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('strategy', 'agent', 'execution', 'manual')),
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

INTRA_DAY_DDL = """
CREATE TABLE IF NOT EXISTS intraday_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_time TEXT NOT NULL,
    check_round TEXT NOT NULL,
    code TEXT NOT NULL,
    check_type TEXT NOT NULL,
    current_price REAL NOT NULL,
    trigger_price REAL,
    status TEXT NOT NULL CHECK (status IN ('no_action', 'triggered', 'skipped', 'error', 'blocked')),
    detail TEXT,
    latency_ms INTEGER,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

EXIT_TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS post_exit_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    exit_date TEXT NOT NULL,
    exit_price REAL NOT NULL,
    entry_price REAL NOT NULL,
    total_return_pct REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    check_point TEXT NOT NULL CHECK (check_point IN ('T+5', 'T+10', 'T+20')),
    check_date TEXT,
    current_price REAL,
    price_change_since_exit REAL,
    quality TEXT CHECK (quality IN ('correct', 'neutral', 'wrong', NULL)),
    quality_detail TEXT,
    review_note TEXT,
    created_at TEXT DEFAULT (datetime('now', '+8 hours')),
    UNIQUE (code, exit_date, check_point)
)
"""

# D5 通电: 盘中盯市快照落库 (原 _record_pnl_snapshot 纯内存, cron 模式每进程仅1条)
INTRADAY_PNL_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS intraday_pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_time TEXT NOT NULL,
    unrealized_pnl_pct REAL,
    active_count INTEGER,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

# EX-3: 日终持仓快照 (期4 半通电面根治 — stock_pool 只有现值无历史)。
# 日终 (fx_settlement 18:05 窗) 落一行/仓: date+code 幂等, 重跑覆盖当日
# (晚到的日终价最后一跑为准); weight = 该仓 mv / Σ(active mv) 仓内占比。
PAPER_POSITIONS_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS paper_positions_snapshot (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    shares INTEGER,
    mv REAL,
    weight REAL,
    entry_price REAL,
    unrealized_pnl REAL,
    created_at TEXT DEFAULT (datetime('now', '+8 hours')),
    PRIMARY KEY (date, code)
)
"""

EXECUTION_STATS_DDL = """
CREATE TABLE IF NOT EXISTS execution_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL CHECK (period IN ('daily', 'weekly', 'monthly', 'all')),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    total_recommendations INTEGER DEFAULT 0,
    executed_entries INTEGER DEFAULT 0,
    entry_rate REAL,
    total_trades INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    win_rate REAL,
    avg_return REAL,
    total_return REAL,
    max_return REAL,
    min_return REAL,
    avg_holding_days REAL,
    max_holding_days INTEGER,
    stop_loss_count INTEGER DEFAULT 0,
    take_profit_count INTEGER DEFAULT 0,
    trailing_stop_count INTEGER DEFAULT 0,
    technical_exit_count INTEGER DEFAULT 0,
    timeout_count INTEGER DEFAULT 0,
    thesis_break_count INTEGER DEFAULT 0,
    correct_exits INTEGER DEFAULT 0,
    neutral_exits INTEGER DEFAULT 0,
    wrong_exits INTEGER DEFAULT 0,
    exit_accuracy REAL,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

# D1 续卡: 入场拦截记录台账 — check_entry 各闸门拦截结构化留痕。
# 派单要旨「被拦的也要有说法」: gate=哪道闸 (valid_until/protection/
# spike_defense/constraint/agent_review/min_holding), detail=具体判定
# (宁可 NULL 不可假值)。报告面 (午报跟踪段) 经此表渲染被拦原因。
ENTRY_BLOCKS_DDL = """
CREATE TABLE IF NOT EXISTS entry_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_time TEXT NOT NULL,
    code TEXT NOT NULL,
    gate TEXT,
    detail TEXT,
    price REAL,
    entry_low REAL,
    entry_high REAL,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

AGENT_INVALIDATION_WARNINGS_DDL = """
CREATE TABLE IF NOT EXISTS agent_invalidation_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    expert_name TEXT NOT NULL,
    condition TEXT NOT NULL,
    request_id TEXT,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
)
"""

ALL_TABLES = [
    STOCK_POOL_DDL,
    "CREATE INDEX IF NOT EXISTS idx_pool_status ON stock_pool(status);",
    "CREATE INDEX IF NOT EXISTS idx_pool_source ON stock_pool(source);",
    "CREATE INDEX IF NOT EXISTS idx_pool_added_date ON stock_pool(added_date);",
    ACTION_RECORDS_DDL,
    "CREATE INDEX IF NOT EXISTS idx_actions_code ON action_records(code);",
    "CREATE INDEX IF NOT EXISTS idx_actions_date ON action_records(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_actions_type ON action_records(action_type);",
    INTRA_DAY_DDL,
    "CREATE INDEX IF NOT EXISTS idx_checks_time ON intraday_checks(check_time);",
    "CREATE INDEX IF NOT EXISTS idx_checks_code ON intraday_checks(code);",
    "CREATE INDEX IF NOT EXISTS idx_checks_type ON intraday_checks(check_type);",
    EXIT_TRACKING_DDL,
    # W1 任务D: 防重复唯一索引 — 重复 create_tracking 曾产出双份 T+5/T+10/T+20
    # 行并双份评估。旧库已有重复行时本语句显性失败 (宁缺勿脏, 不静默删历史),
    # 重复须人工处置后 start() 才通过; 干净旧库与新库自动获得约束。
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_tracking_code_exit_point "
    "ON post_exit_tracking (code, exit_date, check_point);",
    "CREATE INDEX IF NOT EXISTS idx_tracking_code ON post_exit_tracking(code);",
    "CREATE INDEX IF NOT EXISTS idx_tracking_point ON post_exit_tracking(check_point);",
    EXECUTION_STATS_DDL,
    "CREATE INDEX IF NOT EXISTS idx_stats_period ON execution_stats(period);",
    INTRADAY_PNL_SNAPSHOTS_DDL,
    "CREATE INDEX IF NOT EXISTS idx_pnl_snap_time ON intraday_pnl_snapshots(snapshot_time);",
    ENTRY_BLOCKS_DDL,
    "CREATE INDEX IF NOT EXISTS idx_blocks_code ON entry_blocks(code);",
    PAPER_POSITIONS_SNAPSHOT_DDL,
    "CREATE INDEX IF NOT EXISTS idx_pps_date ON paper_positions_snapshot(date);",
    AGENT_INVALIDATION_WARNINGS_DDL,
    "CREATE INDEX IF NOT EXISTS idx_inval_code_date ON agent_invalidation_warnings(code, date);",
]


def init_execution_tables(conn):
    for ddl in ALL_TABLES:
        conn.execute(ddl)
    conn.commit()

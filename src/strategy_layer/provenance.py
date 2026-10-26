"""策略运行溯源记录

实现状态 (ST-04, 2026-08-18): **未启用** — 仅单元测试引用, 生产零调用。
启用条件: ST-13 (provenance 决策链溯源接线) 本批执行 — G33–G45 接线顺序第 2 位,
信号生成处同步写 ProvenanceRecord (建表属写库操作, 需用户 GO)。
依据: 决策账本 S-09 (provenance = 首个接线的 G 件)。
"""
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from common.decorators import catch_quantstar_error


@dataclass
class RunRecord:
    run_id: str
    date: str
    mode: str
    code_commit: str
    config_hash: str
    parameters: dict[str, Any] = field(default_factory=dict)
    results_hash: str = ""
    stock_count: int = 0
    signal_count: int = 0
    duration_s: float = 0.0
    status: str = "running"
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


@dataclass
class SignalProvenanceRecord:
    """ST-13: 单条信号的决策链快照 — 回答"某日为何对该股产出此信号"。

    绑定产出该信号时的全部决策上下文: 因子贡献 top-N、权重表版本、模型版本、
    regime、Agent 审核结论、composite/ml 分数。
    """
    code: str
    date: str
    action: str
    signal_id: str = ""
    composite_score: float = 0.0
    ml_score: float = 0.0
    regime: str = ""
    model_version: str = ""
    weights_hash: str = ""
    factor_contributions: dict[str, float] = field(default_factory=dict)  # top-N {factor: weight*score}
    agent_confidence: float | None = None
    agent_reason: str = ""
    run_id: str = ""


# AUD-002 警示: 下面 DDL 的时间列默认是 **UTC** (CURRENT_TIMESTAMP /
# datetime('now')), 与全库 BJT 主流 (datetime('now','+8 hours')) 不同。
# 加日期过滤、时间窗查询或渲染给人看之前 **必须先转换**, 否则差 8 小时。
# 禁止只把这里的默认值改成 BJT —— 那会让新行 BJT、存量行 UTC 落进同一列,
# 从「一致的 UTC(安全)」退化成「同列混写(静默出错)」; 要改须连存量一起平移。
# 定谳与三路方案见 docs/audit/OPUS5_DEEP_AUDIT.md 的 AUD-002 段。
class ProvenanceTracker:
    """Strategy run provenance tracking"""

    def __init__(self, db=None):
        self.db = db
        self._ensure_table()

    def _ensure_table(self):
        if self.db is None:
            return
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_runs (
                run_id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                mode TEXT DEFAULT 'live',
                code_commit TEXT,
                config_hash TEXT,
                parameters TEXT,
                results_hash TEXT,
                results_json TEXT,
                stock_count INTEGER DEFAULT 0,
                signal_count INTEGER DEFAULT 0,
                duration_s REAL DEFAULT 0,
                status TEXT DEFAULT 'running',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ST-13: 信号级决策链溯源表 (建表属写库操作, 需用户 GO 后在生产库生效)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_provenance (
                signal_id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                action TEXT NOT NULL,
                composite_score REAL DEFAULT 0,
                ml_score REAL DEFAULT 0,
                regime TEXT DEFAULT '',
                model_version TEXT DEFAULT '',
                weights_hash TEXT DEFAULT '',
                factor_contributions TEXT DEFAULT '{}',
                agent_confidence REAL,
                agent_reason TEXT DEFAULT '',
                run_id TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_provenance_code_date ON signal_provenance(code, date)")
        # W1 no-stocks-obs: 存量库补齐 results_json (旧库 CREATE IF NOT EXISTS
        # 对已存在的表不动分毫 — 补列显式做, 否则 finish_run UPDATE 撞无列炸)。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_runs)").fetchall()}
        if "results_json" not in cols:
            conn.execute("ALTER TABLE strategy_runs ADD COLUMN results_json TEXT")
        conn.commit()

    @catch_quantstar_error(layer_name="strategy")
    def start_run(self, date: str, mode: str = "live", parameters: dict | None = None) -> str:
        import subprocess

        try:
            commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=os.path.join(os.path.dirname(__file__), "..", "..")).decode().strip()
        except Exception:
            commit = "unknown"

        config_str = json.dumps(parameters or {}, sort_keys=True)
        config_hash = hashlib.md5(config_str.encode()).hexdigest()[:8]

        run_id = f"{date}-{config_hash}-{datetime.now().strftime('%H%M%S')}"

        record = RunRecord(
            run_id=run_id,
            date=date,
            mode=mode,
            code_commit=commit,
            config_hash=config_hash,
            parameters=parameters or {},
        )

        self._save(record)
        return run_id

    @catch_quantstar_error(layer_name="strategy")
    def finish_run(self, run_id: str, status: str = "success",
                   stock_count: int = 0, signal_count: int = 0,
                   duration_s: float = 0.0, results: dict | None = None):
        results_hash = ""
        # W1 no-stocks-obs: results_json 原样落库 (hash 单向不可解码 —
        # 观测键要「事后回看」必须原样落, 不能只落散列)。results=None 时
        # 落空串 (与 results_hash 空串对称, 不掺旧行口径)。
        results_json = json.dumps(results, sort_keys=True) if results is not None else ""
        if results:
            results_hash = hashlib.md5(json.dumps(results, sort_keys=True).encode()).hexdigest()[:8]

        if self.db:
            conn = self.db.get_connection()
            conn.execute(
                "UPDATE strategy_runs SET status=?, stock_count=?, signal_count=?, duration_s=?, results_hash=?, results_json=? WHERE run_id=?",
                (status, stock_count, signal_count, duration_s, results_hash, results_json, run_id),
            )
            conn.commit()

    @catch_quantstar_error(layer_name="strategy")
    def _save(self, record: RunRecord):
        if self.db:
            conn = self.db.get_connection()
            conn.execute(
                "INSERT OR REPLACE INTO strategy_runs (run_id, date, mode, code_commit, config_hash, parameters, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record.run_id, record.date, record.mode, record.code_commit, record.config_hash,
                 json.dumps(record.parameters), record.status),
            )
            conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        if self.db:
            conn = self.db.get_connection()
            row = conn.execute("SELECT * FROM strategy_runs WHERE run_id=?", (run_id,)).fetchone()
            if row:
                return dict(row)
        return None

    def list_runs(self, limit: int = 10) -> list[dict]:
        if self.db:
            conn = self.db.get_connection()
            rows = conn.execute("SELECT * FROM strategy_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        return []

    # ---- ST-13: 信号级决策链溯源 ----

    @catch_quantstar_error(layer_name="strategy")
    def record_signal_provenance(self, record: SignalProvenanceRecord):
        """信号生成时同步写一条决策链快照 (每信号一条, 供审计/调试/信号可解释)。"""
        if self.db is None:
            return
        conn = self.db.get_connection()
        conn.execute(
            """INSERT OR REPLACE INTO signal_provenance
               (signal_id, code, date, action, composite_score, ml_score, regime,
                model_version, weights_hash, factor_contributions,
                agent_confidence, agent_reason, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.signal_id, record.code, record.date, record.action,
             record.composite_score, record.ml_score, record.regime,
             record.model_version, record.weights_hash,
             json.dumps(record.factor_contributions, ensure_ascii=False),
             record.agent_confidence, record.agent_reason, record.run_id),
        )
        conn.commit()

    def get_signal_provenance(self, code: str, date: str) -> list[dict]:
        """按 code+date 反查当日决策链 (审计/报告层/调试入口)。"""
        if self.db is None:
            return []
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT * FROM signal_provenance WHERE code = ? AND date = ? ORDER BY created_at",
            (code, date),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["factor_contributions"] = json.loads(d.get("factor_contributions") or "{}")
            except Exception:
                pass
            out.append(d)
        return out

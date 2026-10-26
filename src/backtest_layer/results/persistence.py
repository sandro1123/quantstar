"""backtest_layer/results/persistence.py — backtest_runs 落库 + 版本管理 (BT-03)

现状 B-02: 回测结果只打日志/返回内存对象, 无历史可查、无版本对比。
本模块按设计文档 §11 schema 实现 backtest_runs 表的写读, BacktestLayer.run
出口统一落库。

启用条件: 建表属写库操作 — config 开关 backtest.persistence.enabled 默认关闭,
需用户 GO 后启用。未启用时 BacktestLayer 不建表不写库, 行为与修复前一致。

manifest 最小实现: strategy_config_hash (strategy_params 的 SHA256) +
数据日期范围; data_hash (数据快照 SHA256) 后补 — 现阶段无数据快照机制,
该列写 NULL 而非编造指纹 (宁可没有, 不可是假的)。
"""

import hashlib
import json
import uuid

from common.logging import get_logger
from common.utils import bj_now

logger = get_logger("backtest.persistence")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    strategy_version TEXT,
    strategy_config_hash TEXT,
    data_hash TEXT,
    date_range_start TEXT,
    date_range_end TEXT,
    universe_size INTEGER,
    total_return REAL,
    annual_return REAL,
    sharpe REAL,
    max_drawdown REAL,
    win_rate REAL,
    total_trades INTEGER,
    benchmark_return REAL,
    excess_return REAL,
    information_ratio REAL,
    wf_efficiency REAL,
    wf_consistency REAL,
    created_at TEXT,
    duration_ms INTEGER,
    manifest_json TEXT,
    ic_adjustments TEXT,
    param_adjustments TEXT
)
"""


def config_hash(strategy_params: dict | None) -> str | None:
    """strategy_params 的 SHA256 — 空/缺失时 None (不编造指纹)"""
    if not strategy_params:
        return None
    payload = json.dumps(strategy_params, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BacktestRunStore:
    """backtest_runs 表写读 — db 为 common.database.Database 实例"""

    def __init__(self, db):
        self.db = db

    def ensure_table(self):
        conn = self.db.get_connection()
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_runs_mode_date "
            "ON backtest_runs(mode, date_range_start)")
        conn.commit()

    def save(self, report, bt_input=None, duration_ms: int | None = None) -> str:
        """落库一条回测记录, 返回 run_id。

        report: BacktestReport; bt_input: 产生该报告的 BacktestInput (供 manifest,
        组合聚合等无单一 input 的场景可省略)。
        """
        self.ensure_table()
        run_id = report.run_id or uuid.uuid4().hex
        params = getattr(bt_input, "strategy_params", None) if bt_input else None
        wf = report.wf_result or {}
        # G7 (2026-10-23): 键名对齐 —— walk_forward 产出的是 `oos_efficiency`,
        # 本层却读 `efficiency` => wf_efficiency 永远 NULL => 门禁永远「未评估」。
        # 生产者(walk_forward.py:141)与消费者(此处)用不同键名,
        # 两边各自有测试却没人验「生产者产出的键, 消费者读得到」。
        #
        # 兼容读: 优先 oos_efficiency(生产者的真键), 回落 efficiency(历史)。
        _wf_eff = wf.get("oos_efficiency", wf.get("efficiency"))
        _wf_con = wf.get("stability_score", wf.get("consistency"))
        manifest = {
            "strategy_config_hash": config_hash(params),
            "data_date_range": list(report.date_range) if report.date_range else None,
            "strategy_name": report.strategy_name or None,
            # data_hash: 数据快照 SHA256 — 无快照机制, 后补 (不编造)
            "data_hash": None,
        }
        conn = self.db.get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO backtest_runs ("
            "run_id, mode, strategy_version, strategy_config_hash, data_hash, "
            "date_range_start, date_range_end, universe_size, "
            "total_return, annual_return, sharpe, max_drawdown, win_rate, total_trades, "
            "benchmark_return, excess_return, information_ratio, "
            "wf_efficiency, wf_consistency, created_at, duration_ms, manifest_json, "
            "ic_adjustments, param_adjustments"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                report.mode,
                (params or {}).get("version") if params else None,
                manifest["strategy_config_hash"],
                None,
                report.date_range[0] if report.date_range else None,
                report.date_range[1] if report.date_range else None,
                len(bt_input.codes) if bt_input and bt_input.codes else None,
                report.total_return,
                report.annual_return,
                report.sharpe,
                report.max_drawdown,
                report.win_rate,
                report.total_trades,
                report.benchmark_return,
                report.excess_return,
                report.information_ratio,
                _wf_eff,
                _wf_con,
                report.created_at or bj_now(),
                duration_ms,
                json.dumps(manifest, ensure_ascii=False),
                None,
                None,
            ),
        )
        conn.commit()
        logger.info(f"backtest_runs saved: run_id={run_id} mode={report.mode} "
                    f"range={report.date_range}")
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, mode: str | None = None, limit: int = 20) -> list[dict]:
        conn = self.db.get_connection()
        if mode:
            rows = conn.execute(
                "SELECT * FROM backtest_runs WHERE mode = ? "
                "ORDER BY created_at DESC LIMIT ?", (mode, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def latest_run(self, mode: str | None = None) -> dict | None:
        runs = self.list_runs(mode=mode, limit=1)
        return runs[0] if runs else None

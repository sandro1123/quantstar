# -*- coding: utf-8 -*-
"""#101 返工 P1: _run_chart_analysis 时区双加 8h 修复的行为钉 (TDD)。

病 (统筹 sm-b695196e1fe148a3, C 独立审查 P1): orchestrator.py:1889
`(datetime.now() + timedelta(hours=8)).strftime(...)` — 服务器
TZ=Asia/Shanghai, datetime.now() 已是 CST, python 层再 +8h = UTC+16
双加。读图挂载 = 信号生成后的午后批, 任何 ≥16:00 CST 触发都把
date 推到次日 → daily_klines 无次日 K → rows[-1][0] != date 全候选
静默跳过 → analyzed=0 但 success=True (静默跳过无痕族, 铁律 #12),
次日 issues「读图断电 warning」才暴露。病根 = sqlite
datetime('now', '+8 hours') UTC 存储惯用法被逐字复制进 python
本地时间语境 (跨语言惯例拷贝)。

本测试 = 统筹令的判定时刻钉: monkeypatch datetime 模块属性 (handler
函数局部 `from datetime import datetime` 调用时读模块属性) 冻结
now() = 2026-09-03 17:05 CST, 断言派生 date=当日:
  - 候选真被分析: metadata analyzed=1 (+8h 形态 = 0, 此行红);
  - chart_analysis 当日 2 行 (atr_floor 基线 + llm degraded 双录),
    次日 0 行 (+8h 双加若复燃, 行落次日 = 本条转红)。
红-绿: 撤回修复 (恢复 +8h) 时本文件必须失败。
"""
import datetime as _dt
from types import SimpleNamespace

from agent_layer.chart_reader.repository import ChartAnalysisRepository
from common.database import Database
from execution_layer.migrations import ensure_schema
from ops_layer.alert_manager import AlertManager
from ops_layer.orchestrator import PipelineOrchestrator
from ops_layer.pipeline_logger import PipelineLogger

# ── helpers (切2 test_chart_reader_leg_b 同款拷贝: 同一 fixture 形状
#    保证 analyze 的 ATR/最低根数守卫全部放行) ──


def _mk_db() -> Database:
    db = Database(":memory:")
    db.init_db()
    ensure_schema(db)
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL, pre_close REAL, change_pct REAL,
            turnover_rate REAL, adjust_type TEXT
        )""")
    # 测试专用的精简列集 (不与生产列集对齐, 勿模仿);
    # signal_provenance 由 ProvenanceTracker ctor 建; stock_pool 由 STOCK_POOL_DDL
    from strategy_layer.provenance import ProvenanceTracker
    ProvenanceTracker(db)
    from execution_layer.tables import STOCK_POOL_DDL
    conn.execute(STOCK_POOL_DDL)
    # OPS 表: orchestrator 构造前置 (analyst wiring 测试同序)
    from ops_layer.core.tables import OPS_TABLES_DDL
    conn.executescript(OPS_TABLES_DDL)
    conn.commit()
    return db


def _insert_klines(conn, code, rows):
    """rows: list[(date, open, high, low, close, volume)] — 首列带日期 open。"""
    conn.executemany(
        "INSERT INTO daily_klines (code, date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(code, d, o, h, l, c, v) for (d, o, h, l, c, v) in rows],
    )
    conn.commit()


def _uniform_rows(n=20, start="2026-08-01"):
    """n 根等差日: o=10.2 h=11.0 l=10.0 c=10.5 v=1000 → 每根 ATR=1.0, 收盘±10.5。"""
    d0 = _dt.date.fromisoformat(start)
    return [((d0 + _dt.timedelta(days=i)).isoformat(), 10.2, 11.0, 10.0, 10.5, 1000)
            for i in range(n)]


_FROZEN_NOW = _dt.datetime(2026, 9, 3, 17, 5)   # 17:05 CST — 统筹判定时刻


class _FrozenDatetime(_dt.datetime):
    """datetime.now() → 17:05 CST 冻结钟; 其余行为全继承真 datetime。"""

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return _FROZEN_NOW.replace(tzinfo=tz)
        return _FROZEN_NOW


class TestChartAnalysisHandlerDate:
    def test_1705cst_trigger_derives_same_day_date(self, monkeypatch):
        """17:05 CST 触发 (cfg 无 date) → 派生 date=当日 2026-09-03, 候选真被分析。"""
        db = _mk_db()
        conn = db.get_connection()
        # K 线尾 = 2026-09-03 (冻结当日), 20 根 (最低根数守卫)
        _insert_klines(conn, "000001", _uniform_rows(20, start="2026-08-15"))
        conn.execute(
            "INSERT INTO signal_provenance (signal_id, code, date, action) "
            "VALUES ('s1', '000001', '2026-09-03', 'buy')")
        conn.commit()

        # handler 函数局部 import 在调用时读模块属性 → 模块属性即生效面
        import bootstrap
        monkeypatch.setattr(bootstrap, "bootstrap_app", lambda: SimpleNamespace())
        monkeypatch.setattr(_dt, "datetime", _FrozenDatetime)

        orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
        result = orch._run_chart_analysis({})    # {} → 走派生分支

        # +8h 双加形态 (date=2026-09-04): 候选被静默跳过 → analyzed=0 → 此行红
        assert result.metadata == {"analyzed": 1, "llm_ok": 0, "degraded": 1}
        repo = ChartAnalysisRepository(db)
        assert repo.count_for_date("2026-09-03") == 2    # 当日双录 (基线+degraded)
        assert repo.count_for_date("2026-09-04") == 0   # 行未被 +8h 推到次日
        db.close()

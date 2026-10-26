# -*- coding: utf-8 -*-
"""#101 切2 Leg B — chart_reader LLM腿+ATR护栏 RED (TDD)

设计稿 §3 Leg B (md5 3e477fab71ad07c7caed760be26da7c5):
- 输入 = 60日OHLCV结构化摘要 (数字非图片) + Leg A levels + 现ATR带
- 输出 schema (强校验JSON): {pattern, entry_zone[low,high], stop, profit, confidence∈[0,1], reasons[]}
- 确定性底线 (铁律): ATR带=护栏不是替换 —
  * entry_zone 整带 ⊆ [price−0.5×ATR, price+0.5×ATR]
  * stop 距 entry_mid ≥1×ATR 且 ≤3×ATR (做多: stop 必在 entry 下方)
  * profit − entry_mid ≥ 2×ATR
  * 越界或 schema 不过 → fallback 纯ATR公式值 + 标记 degraded
  * LLM 永不进打分公式; 09 永不写 signal/pending_entry (§1.5 唯一写者=02)

degraded 行语义 (本卡裁定, 回执报统筹):
- llm_chart 行仍写 (双录完整性: LLM 尝试必留痕), 但 pattern=NULL /
  confidence=NULL / entry,stop,profit=纯ATR公式值 / reasons=JSON数组携带
  "degraded"+违规码 / llm_raw=原始LLM输出(若有)。
- 判别键: llm_chart 行 pattern IS NULL ⇔ degraded (schema 紧, 不加列)。

LLM 通道: app.agent.get_llm_service().call_structured (AG-10 统一入口,
chat_structured 2-attempt + dataclass 强校验先例, mock 皆可注入)。
hermetic: 全程 mock, 不真调 9router。
"""
import inspect
import json
from datetime import date as _date, timedelta
from unittest.mock import MagicMock

import pytest

from common.database import Database
from agent_layer.chart_reader.repository import ChartAnalysisRepository
from agent_layer.chart_reader.leg_b import (
    ChartConclusion,
    ChartLLMAnalyzer,
    build_ohlcv_summary,
    compute_atr,
    load_candidates,
    validate_conclusion,
)
from execution_layer.migrations import ensure_schema


# ---------------------------------------------------------------- helpers
def _mk_db():
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
    # 候选两表走生产建表入口 (不夹具自造列名, 56卡坑):
    # signal_provenance ← ProvenanceTracker ctor; stock_pool ← STOCK_POOL_DDL
    from strategy_layer.provenance import ProvenanceTracker
    ProvenanceTracker(db)
    from execution_layer.tables import STOCK_POOL_DDL
    conn.execute(STOCK_POOL_DDL)
    conn.commit()
    return db


def _insert_klines(conn, code, rows):
    """rows: list[(date, open, high, low, close, volume)] — 生产列面含 open"""
    conn.executemany(
        "INSERT INTO daily_klines (code, date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(code, d, o, h, l, c, v) for (d, o, h, l, c, v) in rows],
    )
    conn.commit()


def _uniform_rows(n=20, start="2026-08-01"):
    """n 根均匀K线: o=10.2 h=11.0 l=10.0 c=10.5 v=1000 → ATR=1.0, 现价10.5"""
    d0 = _date.fromisoformat(start)
    return [((d0 + timedelta(days=i)).isoformat(), 10.2, 11.0, 10.0, 10.5, 1000)
            for i in range(n)]


def _valid_conclusion():
    """合法结论: 带内 entry [10.2,10.8] / stop 距2.0 / profit 跨3.0 / conf 0.7"""
    return ChartConclusion(pattern="缩量回踩突破", entry_zone=[10.2, 10.8],
                           stop=8.5, profit=13.5, confidence=0.7,
                           reasons=["回踩缩量", "突破放量"])


# ================================================================ 组6: schema + 摘要 + ATR
class TestSchemaAndSummary:
    def test_chart_conclusion_schema_fields(self):
        """§3 输出 schema 五字段: pattern/entry_zone/stop/profit/confidence/reasons"""
        import dataclasses
        names = {f.name for f in dataclasses.fields(ChartConclusion)}
        assert names == {"pattern", "entry_zone", "stop", "profit",
                          "confidence", "reasons"}

    def test_build_ohlcv_summary_window(self):
        """80根K线 → 取最后60根: 窗外高点不入摘要, last_close/高/低/均量全对"""
        rows = []
        d0 = _date(2026, 6, 1)
        for i in range(80):
            d = (d0 + timedelta(days=i)).isoformat()
            if i == 5:
                rows.append((d, 10.0, 20.0, 9.0, 10.5, 100))   # 窗外伪高点
            elif i == 70:
                rows.append((d, 10.0, 15.0, 9.0, 10.5, 100))   # 窗内真高点
            elif i == 79:
                rows.append((d, 11.0, 12.0, 10.8, 12.0, 100))  # 末日
            else:
                rows.append((d, 10.0, 11.0, 9.0, 10.5, 100))
        s = build_ohlcv_summary(rows, limit=60)
        assert s["window_days"] == 60
        assert len(s["rows"]) == 60
        assert s["last_close"] == 12.0
        assert s["window_high"] == 15.0     # 20.0 在窗外被截断
        assert s["window_low"] == 9.0
        assert s["avg_volume"] == pytest.approx(100.0)

    def test_compute_atr(self):
        """ATR = 14窗 (high-low) 均值 round4 (02 同式); 不足14根 → None"""
        rows = _uniform_rows(20)
        assert compute_atr(rows) == 1.0
        assert compute_atr(_uniform_rows(13)) is None


# ================================================================ 组7: ATR 护栏
class TestGuardrails:
    """五规: 带内 entry / stop 距离窗 [1,3]×ATR / profit ≥2×ATR / conf∈[0,1] /
    schema 不过。violation → fallback 公式值 (price=10.5, atr=1.0 基准)。"""

    PRICE, ATR = 10.5, 1.0
    FORMULA = {"entry_low": 10.0, "entry_high": 11.0, "stop": 8.5, "profit": 13.5}

    def test_valid_conclusion_passes(self):
        v = validate_conclusion(_valid_conclusion(), self.PRICE, self.ATR)
        assert v["ok"] is True
        assert v["violations"] == []
        assert v["values"] == {"entry_low": 10.2, "entry_high": 10.8,
                               "stop": 8.5, "profit": 13.5, "confidence": 0.7}

    def test_entry_zone_out_of_band_rejected(self):
        c = _valid_conclusion()
        c.entry_zone = [11.2, 11.6]
        v = validate_conclusion(c, self.PRICE, self.ATR)
        assert v["ok"] is False
        assert v["violations"] == ["entry_out_of_band"]
        assert v["values"] == {**self.FORMULA, "confidence": None}

    def test_entry_zone_straddles_edge_rejected(self):
        """整带语义: low 出界即拒 (哪怕 high 在带内)"""
        c = _valid_conclusion()
        c.entry_zone = [9.8, 10.4]
        v = validate_conclusion(c, self.PRICE, self.ATR)
        assert v["ok"] is False
        assert v["violations"] == ["entry_out_of_band"]

    def test_stop_too_close_rejected(self):
        c = _valid_conclusion()
        c.stop = 10.0   # 距 entry_mid 0.5 < 1×ATR
        v = validate_conclusion(c, self.PRICE, self.ATR)
        assert v["violations"] == ["stop_too_close"]

    def test_stop_too_far_rejected(self):
        c = _valid_conclusion()
        c.stop = 6.0    # 距 4.5 > 3×ATR
        v = validate_conclusion(c, self.PRICE, self.ATR)
        assert v["violations"] == ["stop_too_far"]

    def test_stop_above_entry_rejected(self):
        """做多语义: stop 必在 entry 下方"""
        c = _valid_conclusion()
        c.stop = 12.0
        v = validate_conclusion(c, self.PRICE, self.ATR)
        assert v["violations"] == ["stop_above_entry"]

    def test_profit_too_small_rejected(self):
        c = _valid_conclusion()
        c.profit = 11.5  # 跨 entry_mid 1.0 < 2×ATR
        v = validate_conclusion(c, self.PRICE, self.ATR)
        assert v["violations"] == ["profit_too_small"]

    def test_confidence_out_of_range_rejected(self):
        c = _valid_conclusion()
        c.confidence = 1.5
        v = validate_conclusion(c, self.PRICE, self.ATR)
        assert v["violations"] == ["confidence_out_of_range"]

    def test_none_conclusion_schema_fail(self):
        """LLM 没给合法实例 (坏JSON/关LLM) → schema_fail + fallback 公式值"""
        v = validate_conclusion(None, self.PRICE, self.ATR)
        assert v["ok"] is False
        assert v["violations"] == ["schema_fail"]
        assert v["values"] == {**self.FORMULA, "confidence": None}


# ================================================================ 组8: 候选并集
class TestCandidateScope:
    def test_load_candidates_union(self):
        """作用域 §4: signal_provenance 当日 ∪ stock_pool 未出场 (excluded 不入)"""
        db = _mk_db()
        conn = db.get_connection()
        conn.executemany(
            "INSERT INTO signal_provenance (signal_id, code, date, action) VALUES (?,?,?,?)",
            [("s1", "000001", "2026-08-20", "buy"),
             ("s2", "600036", "2026-08-20", "buy"),
             ("s3", "000001", "2026-08-19", "buy")],   # 非当日 → 不入
        )
        conn.executemany(
            "INSERT INTO stock_pool (code, name, status, added_date) VALUES (?,?,?,?)",
            [("000001", "平安银行", "active", "2026-07-01"),
             ("300750", "宁德时代", "watch", "2026-07-02"),
             ("601318", "中国平安", "exited", "2026-06-01")],  # 已出场 → 不入
        )
        conn.commit()
        codes = load_candidates(conn, "2026-08-20")
        assert codes == ["000001", "300750", "600036"]   # 去重排序, exited/他日排除
        db.close()


# ================================================================ 组9: Analyzer hermetic 双录
class TestChartLLMAnalyzer:
    def _setup(self):
        db = _mk_db()
        conn = db.get_connection()
        _insert_klines(conn, "000001", _uniform_rows(20))
        conn.execute(
            "INSERT INTO signal_provenance (signal_id, code, date, action) "
            "VALUES ('s1', '000001', '2026-08-20', 'buy')")
        conn.commit()
        return db

    def test_valid_llm_writes_dual_rows(self):
        """LLM 合法 → 双录: atr_floor 公式行 + llm_chart LLM值行"""
        db = self._setup()
        service = MagicMock()
        service.call_structured.return_value = _valid_conclusion()
        result = ChartLLMAnalyzer(db, llm_service=service).analyze("2026-08-20")
        assert result == {"analyzed": 1, "llm_ok": 1, "degraded": 0}
        repo = ChartAnalysisRepository(db)
        assert repo.count_for_date("2026-08-20") == 2
        floor = repo.get("000001", "2026-08-20", "atr_floor")
        assert floor["entry_low"] == 10.0 and floor["entry_high"] == 11.0
        assert floor["stop"] == 8.5 and floor["profit"] == 13.5
        row = repo.get("000001", "2026-08-20", "llm_chart")
        assert row["pattern"] == "缩量回踩突破"
        assert row["entry_low"] == 10.2 and row["entry_high"] == 10.8
        assert row["stop"] == 8.5 and row["profit"] == 13.5
        assert row["confidence"] == 0.7
        assert json.loads(row["reasons"]) == ["回踩缩量", "突破放量"]
        assert row["llm_raw"] is not None
        assert row["atr_baseline"] == 1.0
        db.close()

    def test_prompt_composition(self):
        """§3 输入三件套: 60日OHLCV摘要 + LegA levels + 现ATR带/现价"""
        db = self._setup()
        service = MagicMock()
        service.call_structured.return_value = _valid_conclusion()
        ChartLLMAnalyzer(db, llm_service=service).analyze("2026-08-20")
        prompt = service.call_structured.call_args[0][0]
        assert "60" in prompt            # 60日窗口标记
        assert "10.5" in prompt          # 现价
        assert "S1" in prompt and "R1" in prompt   # LegA levels
        assert "ATR" in prompt            # ATR 带说明
        db.close()

    def test_bad_json_falls_back_degraded(self):
        """LLM 返回 None (坏JSON 2-attempt 耗尽) → degraded 行: 公式值+标记"""
        db = self._setup()
        service = MagicMock()
        service.call_structured.return_value = None
        result = ChartLLMAnalyzer(db, llm_service=service).analyze("2026-08-20")
        assert result == {"analyzed": 1, "llm_ok": 0, "degraded": 1}
        row = ChartAnalysisRepository(db).get("000001", "2026-08-20", "llm_chart")
        assert row["pattern"] is None                       # 判别键: degraded
        assert row["entry_low"] == 10.0 and row["entry_high"] == 11.0
        assert row["stop"] == 8.5 and row["profit"] == 13.5
        assert row["confidence"] is None
        reasons = json.loads(row["reasons"])
        assert any("degraded" in r for r in reasons)
        assert row["llm_raw"] is None
        db.close()

    def test_violating_conclusion_falls_back_with_forensics(self):
        """越界结论 → degraded + 违规码入 reasons + 原始输出留痕 llm_raw"""
        db = self._setup()
        service = MagicMock()
        bad = _valid_conclusion()
        bad.entry_zone = [11.2, 11.6]
        service.call_structured.return_value = bad
        ChartLLMAnalyzer(db, llm_service=service).analyze("2026-08-20")
        row = ChartAnalysisRepository(db).get("000001", "2026-08-20", "llm_chart")
        assert row["pattern"] is None
        reasons = json.loads(row["reasons"])
        assert "degraded:entry_out_of_band" in reasons
        assert row["llm_raw"] is not None    # 原始越界输出留痕 (对照评估用)
        db.close()

    def test_llm_off_falls_back(self):
        """关LLM (service=None) → 全候选 degraded 公式行, 不抛不静默"""
        db = self._setup()
        result = ChartLLMAnalyzer(db, llm_service=None).analyze("2026-08-20")
        assert result == {"analyzed": 1, "llm_ok": 0, "degraded": 1}
        assert ChartAnalysisRepository(db).count_for_date("2026-08-20") == 2
        db.close()

    def test_no_kline_candidate_skipped(self):
        """候选当日无K线 → 跳过不计 (宁缺毋滥)"""
        db = _mk_db()
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO signal_provenance (signal_id, code, date, action) "
            "VALUES ('s1', '999999', '2026-08-20', 'buy')")
        conn.commit()
        result = ChartLLMAnalyzer(db, llm_service=None).analyze("2026-08-20")
        assert result == {"analyzed": 0, "llm_ok": 0, "degraded": 0}
        db.close()


# ================================================================ 组10: handler 接线
class TestOrchestratorWiring:
    def test_dispatch_wires_chart_analysis(self):
        """dispatch 表挂 _run_chart_analysis (注册-only, 本卡不动 crontab)"""
        from ops_layer.orchestrator import PipelineOrchestrator
        src = inspect.getsource(PipelineOrchestrator)
        assert '"chart_analysis": self._run_chart_analysis' in src

    def test_handler_hermetic_end_to_end(self, monkeypatch):
        """monkeypatch bootstrap_app → 假 app (真db+mock service) → 全链 HandlerResult"""
        import bootstrap
        from ops_layer.orchestrator import PipelineOrchestrator
        from ops_layer.pipeline_logger import PipelineLogger
        from ops_layer.alert_manager import AlertManager

        db = _mk_db()
        conn = db.get_connection()
        _insert_klines(conn, "000001", _uniform_rows(20))
        conn.execute(
            "INSERT INTO signal_provenance (signal_id, code, date, action) "
            "VALUES ('s1', '000001', '2026-08-20', 'buy')")
        conn.commit()

        class _FakeData:
            def __init__(self, db):
                self.db = db

        class _FakeAgent:
            def __init__(self, service):
                self._service = service

            def get_llm_service(self):
                return self._service

        service = MagicMock()
        service.call_structured.return_value = _valid_conclusion()

        class _FakeApp:
            data = None
            agent = None

        app = _FakeApp()
        app.data = _FakeData(db)
        app.agent = _FakeAgent(service)
        monkeypatch.setattr(bootstrap, "bootstrap_app", lambda: app)

        orch = PipelineOrchestrator(db, PipelineLogger(db), AlertManager(db))
        result = orch._run_chart_analysis({"date": "2026-08-20"})
        assert result.success is True
        assert result.metadata == {"analyzed": 1, "llm_ok": 1, "degraded": 0}
        assert ChartAnalysisRepository(db).count_for_date("2026-08-20") == 2
        db.close()

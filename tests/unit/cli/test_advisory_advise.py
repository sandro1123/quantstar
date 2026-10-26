"""advisory advise — 投顾建议 (MVP P1)

verdict 枚举: buy / watch / avoid / stop_loss_warning
规则基于相对排名百分位 (生产 blended_score 是原始模型分, 非 0-100)。
只读 + --json 稳定 schema + 中文化。
"""
import json
import sqlite3

import pytest
from click.testing import CliRunner

from cli.main import cli

TODAY = "2026-08-28"

@pytest.fixture
def adv_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "adv.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT, "
                 "market TEXT, list_date TEXT, is_active INTEGER, delist_date TEXT)")
    conn.execute("CREATE TABLE model_predictions (date TEXT, code TEXT, ml_score REAL, "
                 "factor_score REAL, blended_score REAL)")
    conn.execute("CREATE TABLE stock_pool (code TEXT, status TEXT, entry_low REAL, "
                 "entry_high REAL, stop_loss REAL, take_profit REAL, trigger_price REAL, "
                 "last_price REAL, position_qty REAL, dynamic_stop_loss REAL)")
    conn.execute("CREATE TABLE daily_regime (date TEXT, regime TEXT)")
    # 100 只股票分数 0.0~0.99, 便于算百分位
    for i in range(100):
        code = f"{i:06d}"
        conn.execute("INSERT INTO stock_info VALUES (?,?,?,?,?,1,NULL)",
                     (code, f"股票{i}", "测试", "SH", "2020-01-01"))
        conn.execute("INSERT INTO model_predictions VALUES (?,?,?,?,?)",
                     (TODAY, code, i / 100.0, i / 100.0, i / 100.0))
    # 000099 = 分数 0.99 → top 1% (高分)
    # 000050 = 分数 0.50 → 中位
    # 000001 = 分数 0.01 → 低分
    # 持仓股: 000090 已持仓, last_price 触及 stop_loss
    conn.execute("INSERT INTO stock_pool VALUES ('000090','active',10,11,9.5,13,NULL,9.4,1000,NULL)")
    conn.execute("INSERT INTO stock_pool VALUES ('000080','pending_entry',10,11,9.5,13,NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO daily_regime VALUES (?, 'neutral')", (TODAY,))
    conn.commit()
    conn.close()
    import cli.advisory as adv
    monkeypatch.setattr(adv, "_resolve_db_path", lambda: db_path)
    monkeypatch.setattr(adv, "_today", lambda: TODAY)
    return db_path

def _invoke(args, json_mode=True):
    prefix = ["--json"] if json_mode else []
    return CliRunner().invoke(cli, prefix + args)

def _json(result):
    return json.loads(result.output)

class TestAdvise:
    def test_high_score_not_in_pool_watch(self, adv_db):
        """高分但不在池 → watch (等信号)"""
        d = _json(_invoke(["advisory", "advise", "000099"]))
        assert d["ok"] is True
        assert d["verdict"] == "watch"
        assert d["reasons_zh"]
        assert d["confidence"] > 0

    def test_low_score_avoid(self, adv_db):
        """低分 → avoid"""
        d = _json(_invoke(["advisory", "advise", "000001"]))
        assert d["ok"] is True
        assert d["verdict"] == "avoid"
        assert d["reasons_zh"]

    def test_pending_entry_buy(self, adv_db):
        """在池 pending_entry 且分数不低 → buy (参照入场区间)"""
        d = _json(_invoke(["advisory", "advise", "000080"]))
        assert d["ok"] is True
        assert d["verdict"] == "buy"
        assert d["entry_plan"]["low"] == 10
        assert d["entry_plan"]["high"] == 11

    def test_stop_loss_warning(self, adv_db):
        """已持仓且 last_price <= stop_loss → stop_loss_warning"""
        d = _json(_invoke(["advisory", "advise", "000090"]))
        assert d["ok"] is True
        assert d["verdict"] == "stop_loss_warning"
        assert d["position_context"]["already_held"] is True

    def test_schema_fields(self, adv_db):
        d = _json(_invoke(["advisory", "advise", "000099"]))
        assert d["schema_version"] == 1
        assert d["command"] == "advisory advise"
        for k in ("code", "verdict", "confidence", "reasons_zh", "risks_zh",
                  "entry_plan", "position_context"):
            assert k in d
        assert d["verdict"] in ("buy", "watch", "avoid", "stop_loss_warning")

    def test_not_found(self, adv_db):
        r = _invoke(["advisory", "advise", "999999"])
        assert r.exit_code == 1
        assert _json(r)["error"]["code"] == "STOCK_NOT_FOUND"

    def test_human_output_chinese(self, adv_db):
        r = _invoke(["advisory", "advise", "000099"], json_mode=False)
        assert r.exit_code == 0
        assert "建议" in r.output or "观望" in r.output or "买" in r.output

"""advisory candidates — 今日候选 topN (MVP P1)

model_predictions 最新日期 blended_score topN + stock_pool 去重标记。
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
    conn.execute("CREATE TABLE stock_pool (code TEXT, status TEXT)")
    conn.execute("CREATE TABLE daily_regime (date TEXT, regime TEXT)")
    for i in range(30):
        code = f"{i:06d}"
        conn.execute("INSERT INTO stock_info VALUES (?,?,?,?,?,1,NULL)",
                     (code, f"股票{i}", "测试", "SH", "2020-01-01"))
        conn.execute("INSERT INTO model_predictions VALUES (?,?,?,?,?)",
                     (TODAY, code, i / 30.0, i / 30.0, i / 30.0))
    # 旧日期数据 (不应入选 — 只取最新日期)
    conn.execute("INSERT INTO model_predictions VALUES ('2026-08-01','000000',9.9,9.9,9.9)")
    # 000029 (最高分) 已在池
    conn.execute("INSERT INTO stock_pool VALUES ('000029','pending_entry')")
    conn.execute("INSERT INTO daily_regime VALUES (?, 'bull')", (TODAY,))
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

class TestCandidates:
    def test_default_top10(self, adv_db):
        d = _json(_invoke(["advisory", "candidates"]))
        assert d["ok"] is True
        assert len(d["candidates"]) == 10
        # 降序: 最高分 000029 排第一
        assert d["candidates"][0]["code"] == "000029"
        scores = [c["blended_score"] for c in d["candidates"]]
        assert scores == sorted(scores, reverse=True)

    def test_top_option(self, adv_db):
        d = _json(_invoke(["advisory", "candidates", "--top", "5"]))
        assert len(d["candidates"]) == 5

    def test_in_pool_flag(self, adv_db):
        d = _json(_invoke(["advisory", "candidates"]))
        top = d["candidates"][0]
        assert top["in_pool"] is True
        assert d["candidates"][1]["in_pool"] is False

    def test_name_joined(self, adv_db):
        d = _json(_invoke(["advisory", "candidates"]))
        assert d["candidates"][0]["name"] == "股票29"

    def test_schema_fields(self, adv_db):
        d = _json(_invoke(["advisory", "candidates"]))
        assert d["schema_version"] == 1
        assert d["command"] == "advisory candidates"
        assert d["data_asof"] == TODAY
        assert d["total_screened"] == 30
        assert "regime_zh" in d
        c = d["candidates"][0]
        for k in ("code", "name", "blended_score", "rank", "in_pool"):
            assert k in c

    def test_no_predictions_honest(self, adv_db, tmp_path):
        """无预测数据 → ok:false DATA_MISSING, 不返回空假候选"""
        import sqlite3 as s
        conn = s.connect(adv_db)
        conn.execute("DELETE FROM model_predictions")
        conn.commit()
        conn.close()
        r = _invoke(["advisory", "candidates"])
        assert r.exit_code == 1
        d = _json(r)
        assert d["ok"] is False
        assert d["error"]["code"] == "DATA_MISSING"

    def test_human_output_chinese(self, adv_db):
        r = _invoke(["advisory", "candidates"], json_mode=False)
        assert r.exit_code == 0
        assert "候选" in r.output

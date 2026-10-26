"""advisory analyze — 个股综合分析 (MVP P1)

数据源: factor_daily(估值+技术) + model_predictions(评分) + stock_pool(池状态)
+ stock_news(消息) + daily_regime(大盘)。只读 + --json 稳定 schema + 中文化。
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
    conn.execute("INSERT INTO stock_info VALUES ('600519','贵州茅台','白酒','SH','2001-08-27',1,NULL)")
    conn.execute("INSERT INTO stock_info VALUES ('000001','平安银行','银行','SZ','1991-04-03',1,NULL)")
    conn.execute("CREATE TABLE factor_daily (date TEXT, code TEXT, pe_ttm REAL, pb REAL, "
                 "roe REAL, momentum_5d REAL, momentum_20d REAL, volatility_20d REAL, "
                 "volume_ratio REAL, rsi_14 REAL)")
    conn.execute("INSERT INTO factor_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (TODAY, "600519", 25.3, 7.8, 31.2, 0.021, 0.045, 0.18, 1.2, 55.2))
    conn.execute("CREATE TABLE model_predictions (date TEXT, code TEXT, ml_score REAL, "
                 "factor_score REAL, blended_score REAL)")
    conn.execute("INSERT INTO model_predictions VALUES (?,?,?,?,?)",
                 (TODAY, "600519", 80.1, 76.2, 78.5))
    conn.execute("CREATE TABLE stock_pool (code TEXT, status TEXT, entry_low REAL, "
                 "entry_high REAL, stop_loss REAL)")
    conn.execute("INSERT INTO stock_pool VALUES ('600519','pending_entry',1450,1500,1380)")
    conn.execute("CREATE TABLE stock_news (code TEXT, date TEXT, title TEXT)")
    conn.execute("INSERT INTO stock_news VALUES ('600519','2026-08-27','茅台中报超预期')")
    conn.execute("CREATE TABLE daily_regime (date TEXT, regime TEXT)")
    conn.execute("INSERT INTO daily_regime VALUES (?, 'neutral')", (TODAY,))
    conn.commit()
    conn.close()
    import cli.advisory as adv
    monkeypatch.setattr(adv, "_resolve_db_path", lambda: db_path)
    monkeypatch.setattr(adv, "_today", lambda: TODAY)  # 锁死"今天", 防测试日期漂移
    return db_path

def _invoke(args, json_mode=True):
    prefix = ["--json"] if json_mode else []
    return CliRunner().invoke(cli, prefix + args)

def _json(result):
    return json.loads(result.output)

class TestAnalyze:
    def test_full_data(self, adv_db):
        r = _invoke(["advisory", "analyze", "600519"])
        assert r.exit_code == 0
        d = _json(r)
        assert d["ok"] is True
        assert d["code"] == "600519"
        assert d["name"] == "贵州茅台"
        assert d["score"]["blended"] == 78.5
        assert d["valuation"]["pe_ttm"] == 25.3
        assert d["technicals"]["rsi_14"] == 55.2
        assert d["pool_status"]["in_pool"] is True
        assert d["pool_status"]["status"] == "pending_entry"
        assert d["recent_news"][0]["title"] == "茅台中报超预期"
        assert d["regime"]["market"] == "neutral"
        assert d["summary_zh"]

    def test_schema_fields(self, adv_db):
        d = _json(_invoke(["advisory", "analyze", "600519"]))
        assert d["schema_version"] == 1
        assert d["command"] == "advisory analyze"
        assert d["data_asof"] == TODAY
        for section in ("score", "valuation", "technicals", "pool_status",
                        "recent_news", "regime", "summary_zh"):
            assert section in d

    def test_stock_not_found(self, adv_db):
        r = _invoke(["advisory", "analyze", "999999"])
        assert r.exit_code == 1
        d = _json(r)
        assert d["ok"] is False
        assert d["error"]["code"] == "STOCK_NOT_FOUND"

    def test_missing_score_honest(self, adv_db):
        """000001 无评分/因子数据 → score/valuation 为 null + note, 不造假"""
        d = _json(_invoke(["advisory", "analyze", "000001"]))
        assert d["ok"] is True
        assert d["score"] is None
        assert d["valuation"] is None
        assert d["note_zh"]

    def test_stale_flag(self, adv_db):
        """数据日期早于最新交易日 → stale:true"""
        conn = sqlite3.connect(adv_db)
        # 把 factor_daily/model_predictions 日期改旧
        conn.execute("UPDATE factor_daily SET date='2026-08-20'")
        conn.execute("UPDATE model_predictions SET date='2026-08-20'")
        conn.commit()
        conn.close()
        d = _json(_invoke(["advisory", "analyze", "600519"]))
        assert d["ok"] is True
        assert d["stale"] is True
        assert d["stale_reason_zh"]

    def test_human_output_chinese(self, adv_db):
        r = _invoke(["advisory", "analyze", "600519"], json_mode=False)
        assert r.exit_code == 0
        assert "贵州茅台" in r.output
        assert "综合评分" in r.output or "评分" in r.output


class TestNewFactorForm:
    """N3 (2026-09-14): 89 因子换代后 factor_daily 只写 factor_scores JSON 列,
    物理因子列 (pe_ttm/momentum_5d/rsi_14...) 自 07-21 起全库零写入。

    病理 (侦察档 20260914-C-datagenesis-recon.md N3): 07-18~21 聚合器 20→89 因子
    换代, 写入形态从「物理列 + 20 键 JSON」改为「只写 87 键 JSON 列」。advisory
    analyze 读物理列 → 恒 NULL → 用户面「暂无因子数据」型静默坏 (无告警无降级注记)。
    修法: 估值面三键 (pe_ttm/pb/roe) 切 stock_valuation 当日真值; 技术面五键切
    factor_scores JSON 列 (键名映射: momentum_5d→momentum_5d_reversal 语义反转,
    volatility_20d→volatility_reversal, volume_ratio→turnover_ratio, rsi_14→rsi14)。
    """

    @pytest.fixture
    def n3_db(self, tmp_path, monkeypatch):
        """新形态库: 物理列全 NULL + JSON 列 87 键真值 (07-21 后生产形态)。"""
        db_path = str(tmp_path / "n3.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT, "
                 "market TEXT, list_date TEXT, is_active INTEGER, delist_date TEXT)")
        conn.execute("INSERT INTO stock_info VALUES ('600519','贵州茅台','白酒','SH','2001-08-27',1,NULL)")
        # 新形态 factor_daily: 物理列在表里但恒 NULL (生产 40 列形态的缩影)
        conn.execute("CREATE TABLE factor_daily (date TEXT, code TEXT, pe_ttm REAL, pb REAL, "
                 "roe REAL, momentum_5d REAL, momentum_20d REAL, volatility_20d REAL, "
                 "volume_ratio REAL, rsi_14 REAL, factor_scores TEXT)")
        scores = json.dumps({
            "momentum_20d": 0.045, "momentum_5d_reversal": 0.021,
            "volatility_reversal": 0.18, "turnover_ratio": 1.2, "rsi14": 55.2,
        })
        conn.execute("INSERT INTO factor_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (TODAY, "600519", None, None, None, None, None, None, None, None, scores))
        # 估值面真值源
        conn.execute("CREATE TABLE stock_valuation (code TEXT, date TEXT, pe_ttm REAL, "
                 "pb_mrq REAL, ps_ttm REAL, market_cap REAL, total_shares REAL, dividend_yield REAL)")
        conn.execute("INSERT INTO stock_valuation VALUES ('600519', ?, 25.3, 7.8, 15.1, 2100000.0, 12.56, 3.1)", (TODAY,))
        conn.execute("CREATE TABLE model_predictions (date TEXT, code TEXT, ml_score REAL, "
                 "factor_score REAL, blended_score REAL)")
        conn.execute("INSERT INTO model_predictions VALUES (?,?,?,?,?)", (TODAY, "600519", 80.1, 76.2, 78.5))
        conn.execute("CREATE TABLE stock_pool (code TEXT, status TEXT, entry_low REAL, "
                 "entry_high REAL, stop_loss REAL)")
        conn.execute("CREATE TABLE stock_news (code TEXT, date TEXT, title TEXT)")
        conn.execute("CREATE TABLE daily_regime (date TEXT, regime TEXT)")
        conn.execute("INSERT INTO daily_regime VALUES (?, 'neutral')", (TODAY,))
        conn.commit()
        conn.close()
        import cli.advisory as adv
        monkeypatch.setattr(adv, "_resolve_db_path", lambda: db_path)
        monkeypatch.setattr(adv, "_today", lambda: TODAY)
        return db_path

    def test_technicals_from_json_column(self, n3_db):
        """物理列全 NULL 时技术面从 factor_scores JSON 列取值, 不再恒 None。"""
        d = _json(_invoke(["advisory", "analyze", "600519"]))
        assert d["ok"] is True
        t = d["technicals"]
        assert t is not None, "新形态下 technicals 不应恒 None"
        assert t["momentum_20d"] == 0.045
        assert t["rsi_14"] == 55.2
        assert t["volume_ratio"] == 1.2

    def test_valuation_from_stock_valuation(self, n3_db):
        """估值面三键切 stock_valuation 当日真值 (89 因子体系无原始 PE)。"""
        d = _json(_invoke(["advisory", "analyze", "600519"]))
        v = d["valuation"]
        assert v is not None
        assert v["pe_ttm"] == 25.3
        assert v["pb"] == 7.8

    def test_valuation_missing_when_valuation_table_empty(self, n3_db):
        """stock_valuation 当日无行 (如误杀日断写) → valuation 诚实 None + note, 不造假。"""
        conn = sqlite3.connect(n3_db)
        conn.execute("DELETE FROM stock_valuation")
        conn.commit()
        conn.close()
        d = _json(_invoke(["advisory", "analyze", "600519"]))
        assert d["ok"] is True
        assert d["valuation"] is None
        assert d["note_zh"]

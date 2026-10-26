"""advisory resolve — 名称/代码 → 股票代码 (NL 接入前置, MVP P1)

只读 + --json 稳定 schema + 中文化。红绿测试。
"""
import json
import sqlite3

import pytest
from click.testing import CliRunner

from cli.main import cli

@pytest.fixture
def adv_db(tmp_path, monkeypatch):
    """临时只读库 + stock_info 种子; advisory 的 db 路径解析指向它"""
    db_path = str(tmp_path / "adv.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT, "
                 "market TEXT, list_date TEXT, is_active INTEGER, delist_date TEXT)")
    conn.executemany("INSERT INTO stock_info VALUES (?,?,?,?,?,?,?)", [
        ("600519", "贵州茅台", "白酒", "SH", "2001-08-27", 1, None),
        ("000001", "平安银行", "银行", "SZ", "1991-04-03", 1, None),
        ("000858", "五粮液", "白酒", "SZ", "1998-04-27", 1, None),
        ("600000", "浦发银行", "银行", "SH", "1999-11-10", 1, None),
        ("000002", "万科A", "房地产", "SZ", "1991-01-29", 0, "2026-01-01"),
    ])
    conn.commit()
    conn.close()
    import cli.advisory as adv
    monkeypatch.setattr(adv, "_resolve_db_path", lambda: db_path)
    return db_path

def _invoke(args):
    return CliRunner().invoke(cli, ["--json"] + args)

def _json(result):
    return json.loads(result.output)

class TestResolve:
    def test_exact_code(self, adv_db):
        r = _invoke(["advisory", "resolve", "600519"])
        assert r.exit_code == 0
        d = _json(r)
        assert d["ok"] is True
        assert d["matches"][0]["code"] == "600519"
        assert d["matches"][0]["name"] == "贵州茅台"

    def test_exact_name(self, adv_db):
        d = _json(_invoke(["advisory", "resolve", "贵州茅台"]))
        assert d["ok"] is True
        assert d["matches"][0]["code"] == "600519"

    def test_partial_name(self, adv_db):
        d = _json(_invoke(["advisory", "resolve", "茅台"]))
        assert d["ok"] is True
        assert d["matches"][0]["code"] == "600519"

    def test_multiple_matches(self, adv_db):
        d = _json(_invoke(["advisory", "resolve", "银行"]))
        assert d["ok"] is True
        codes = {m["code"] for m in d["matches"]}
        assert {"000001", "600000"} <= codes

    def test_not_found(self, adv_db):
        r = _invoke(["advisory", "resolve", "不存在的票"])
        assert r.exit_code == 1
        d = _json(r)
        assert d["ok"] is False
        assert d["error"]["code"] == "STOCK_NOT_FOUND"
        assert d["error"]["message_zh"]

    def test_schema_fields(self, adv_db):
        d = _json(_invoke(["advisory", "resolve", "茅台"]))
        assert d["schema_version"] == 1
        assert d["command"] == "advisory resolve"
        assert "data_asof" in d
        m = d["matches"][0]
        for k in ("code", "name", "industry", "is_active"):
            assert k in m

    def test_inactive_flagged(self, adv_db):
        d = _json(_invoke(["advisory", "resolve", "万科"]))
        assert d["ok"] is True
        m = d["matches"][0]
        assert m["is_active"] == 0
        assert m["delist_date"] == "2026-01-01"

    def test_human_output_chinese(self, adv_db):
        r = CliRunner().invoke(cli, ["advisory", "resolve", "茅台"])
        assert r.exit_code == 0
        assert "贵州茅台" in r.output
        assert "600519" in r.output

class TestReadOnly:
    def test_connection_is_readonly(self, adv_db):
        """只读三重保障①: sqlite mode=ro 物理只读 — 写操作必须抛错"""
        import cli.advisory as adv
        conn = adv._ro_connect()
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE hack (x)")
        conn.close()

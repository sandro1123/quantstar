"""DL-30 测试 — 数据目录: 自动派生 (反射 + 注册表), 新鲜度复用 freshness_audit 判据"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from common.database import Database
from data_layer import DataLayer
from data_layer.catalog import (
    build_catalog,
    pipeline_catalog,
    read_api_catalog,
    table_catalog,
)


@pytest.fixture
def layer(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    conn = db.get_connection()
    conn.execute("""CREATE TABLE daily_klines (
        code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, amount REAL, pre_close REAL, change_pct REAL,
        turnover_rate REAL, PRIMARY KEY (code, date))""")
    conn.execute("INSERT INTO daily_klines (code, date) VALUES ('600001', '2026-08-21')")
    conn.execute("""CREATE TABLE stock_info (
        code TEXT PRIMARY KEY, name TEXT, market TEXT)""")
    conn.execute("INSERT INTO stock_info (code, name, market) VALUES ('600001', '测试', 'SH')")
    conn.commit()
    return DataLayer(db=db)


class TestReadApiCatalog:
    def test_known_read_apis_listed(self, layer):
        names = {e["name"] for e in read_api_catalog(layer)}
        for expected in ("snapshot", "get_history", "get_fwd_returns", "check_readiness"):
            assert expected in names, f"缺读 API: {expected}"

    def test_private_and_write_methods_excluded(self, layer):
        names = {e["name"] for e in read_api_catalog(layer)}
        assert not any(n.startswith("_") for n in names)
        assert "shutdown" not in names

    def test_entries_have_signature_and_description(self, layer):
        entries = {e["name"]: e for e in read_api_catalog(layer)}
        snap = entries["snapshot"]
        assert snap["signature"].startswith("snapshot(")
        assert snap["return_type"]
        # docstring 首行自动派生 (get_history 有 docstring; snapshot 无 docstring
        # 且不改 _loader.py 现有方法体——会话C 领地, 描述为空可接受)
        assert entries["get_history"]["description"]


class TestTableCatalog:
    def test_fresh_table_ok(self, layer):
        tables = {t["table"]: t for t in table_catalog(layer.db, "2026-08-21")}
        k = tables["daily_klines"]
        assert k["row_count"] == 1
        assert k["max_date"] == "2026-08-21"
        assert k["days_behind"] == 0
        assert k["status"] == "ok"

    def test_stale_table_delayed(self, layer):
        tables = {t["table"]: t for t in table_catalog(layer.db, "2026-08-22")}
        k = tables["daily_klines"]
        assert k["days_behind"] == 1
        assert k["status"] == "delayed"

    def test_static_table_ok_without_date(self, layer):
        tables = {t["table"]: t for t in table_catalog(layer.db, "2026-08-22")}
        si = tables["stock_info"]
        assert si["status"] == "ok"  # 静态表: 有数据无日期维度 → ok

    def test_missing_table_flagged(self, layer):
        tables = {t["table"]: t for t in table_catalog(layer.db, "2026-08-21")}
        assert tables["stock_financials"]["status"] == "missing"
        assert tables["limit_up_ladder"]["status"] == "missing"


class TestPipelineCatalog:
    def test_pipelines_from_registry(self, layer):
        names = {p["name"] for p in pipeline_catalog(layer)}
        assert "kline" in names
        k = next(p for p in pipeline_catalog(layer) if p["name"] == "kline")
        assert k["produces"]


class TestBuildCatalog:
    def test_stale_tables_listed(self, layer):
        cat = build_catalog(layer, "2026-08-22")
        assert "daily_klines" in cat["stale_tables"]
        assert cat["read_apis"] and cat["tables"] and cat["pipelines"]

    def test_json_serializable(self, layer):
        cat = build_catalog(layer, "2026-08-21")
        json.dumps(cat, ensure_ascii=False)  # 不炸即可


class TestCli:
    def test_catalog_command_json(self, layer, monkeypatch):
        from click.testing import CliRunner
        import cli.main as m
        monkeypatch.setattr(m, "_get_cli_db", lambda: layer.db)
        r = CliRunner().invoke(m.cli, ["--json", "data", "catalog",
                                       "--date", "2026-08-21"])
        assert r.exit_code == 0, r.output
        cat = json.loads(r.output)
        assert cat["audit_date"] == "2026-08-21"
        assert any(e["name"] == "snapshot" for e in cat["read_apis"])

    def test_catalog_command_human(self, layer, monkeypatch):
        from click.testing import CliRunner
        import cli.main as m
        monkeypatch.setattr(m, "_get_cli_db", lambda: layer.db)
        r = CliRunner().invoke(m.cli, ["data", "catalog", "--date", "2026-08-21"])
        assert r.exit_code == 0, r.output
        assert "读 API" in r.output
        assert "daily_klines" in r.output

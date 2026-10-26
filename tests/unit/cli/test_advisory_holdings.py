"""advisory holdings — 推荐跟踪 (MVP P1 + polish 批)

数据源: stock_pool status=active (跟踪中) + exit_pending (待结算单列)。
注意: 不是 paper_trading (0 行, 实测)。只读 + --json 稳定 schema + 中文化。

polish 批 (feat/advisory-polish, 2026-08-28, 接手 E 阵亡未提交改动):
① name 从 stock_info join 取 COALESCE(i.name, p.name) — 根因: stock_pool
   部分股 name 字段存的是代码。
② 今日入场无新价 → note_zh 提示, 禁用昨收/成交价冒充现价 (宁可 NULL 不可假值)。
③ 措辞校准 (F 措辞表): 持仓→推荐跟踪 / 止损位→参考卖出线 / 盈亏→验证收益。
"""
import json
import sqlite3

import pytest
from click.testing import CliRunner

from cli.main import cli

TODAY = "2026-08-28"

_POOL_COLS = ("code,name,status,entry_price,last_price,position_qty,"
              "unrealized_pnl_pct,holding_days,stop_loss,dynamic_stop_loss,"
              "take_profit,exit_reason,entry_date,last_price_date")

@pytest.fixture
def adv_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "adv.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT, "
                 "market TEXT, list_date TEXT, is_active INTEGER, delist_date TEXT)")
    # polish: stock_pool 补 entry_date / last_price_date 列
    conn.execute("CREATE TABLE stock_pool (code TEXT, name TEXT, status TEXT, "
                 "entry_price REAL, last_price REAL, position_qty REAL, "
                 "unrealized_pnl_pct REAL, holding_days INTEGER, stop_loss REAL, "
                 "dynamic_stop_loss REAL, take_profit REAL, exit_reason TEXT, "
                 "entry_date TEXT, last_price_date TEXT)")
    conn.execute("CREATE TABLE daily_klines (code TEXT, date TEXT, close REAL)")
    conn.execute("INSERT INTO stock_info VALUES ('000001','平安银行','银行','SZ','1991-04-03',1,NULL)")
    conn.execute("INSERT INTO stock_info VALUES ('600519','贵州茅台','白酒','SH','2001-08-27',1,NULL)")
    conn.execute("INSERT INTO stock_info VALUES ('000858','五粮液','白酒','SZ','1998-04-27',1,NULL)")
    conn.execute("INSERT INTO stock_info VALUES ('000002','万科A','地产','SZ','1991-01-29',1,NULL)")
    conn.execute("INSERT INTO stock_info VALUES ('300750','宁德时代','电池','SZ','2018-06-11',1,NULL)")
    conn.execute("INSERT INTO stock_info VALUES ('688981','中芯国际','半导体','SH','2020-07-16',1,NULL)")
    # 两只 active (早于今日入场, 有最新价)
    conn.execute(f"INSERT INTO stock_pool ({_POOL_COLS}) VALUES "
                 "('000001','平安银行','active',12.5,13.2,800,5.6,8,11.8,12.2,15.0,NULL,'2026-08-20','2026-08-27')")
    conn.execute(f"INSERT INTO stock_pool ({_POOL_COLS}) VALUES "
                 "('600519','贵州茅台','active',1450.0,1420.0,100,-2.1,3,1380.0,NULL,1600.0,NULL,'2026-08-25','2026-08-27')")
    # 一只 exit_pending
    conn.execute(f"INSERT INTO stock_pool ({_POOL_COLS}) VALUES "
                 "('000858','五粮液','exit_pending',150.0,165.0,200,10.0,15,140.0,NULL,165.0,'触及止盈','2026-08-13','2026-08-27')")
    conn.commit()
    conn.close()
    import cli.advisory as adv
    monkeypatch.setattr(adv, "_resolve_db_path", lambda: db_path)
    monkeypatch.setattr(adv, "_today", lambda: TODAY)
    return db_path

def _add_position(db_path, code, name, entry_price, last_price, entry_date,
                  status="active", stop=None, qty=100, pnl=0.0, days=0):
    conn = sqlite3.connect(db_path)
    conn.execute(f"INSERT INTO stock_pool ({_POOL_COLS}) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                 (code, name, status, entry_price, last_price, qty, pnl, days,
                  stop, None, None, entry_date, entry_date))
    conn.commit()
    conn.close()

def _invoke(args, json_mode=True):
    prefix = ["--json"] if json_mode else []
    return CliRunner().invoke(cli, prefix + args)

def _json(result):
    return json.loads(result.output)

class TestHoldings:
    def test_active_positions(self, adv_db):
        d = _json(_invoke(["advisory", "holdings"]))
        assert d["ok"] is True
        assert len(d["positions"]) == 2
        codes = {p["code"] for p in d["positions"]}
        assert codes == {"000001", "600519"}

    def test_position_fields(self, adv_db):
        d = _json(_invoke(["advisory", "holdings"]))
        p = next(p for p in d["positions"] if p["code"] == "000001")
        assert p["name"] == "平安银行"
        assert p["entry_price"] == 12.5
        assert p["last_price"] == 13.2
        assert p["qty"] == 800
        assert p["unrealized_pnl_pct"] == 5.6
        assert p["holding_days"] == 8
        assert p["stop_loss"] == 11.8
        assert p["dynamic_stop_loss"] == 12.2

    def test_summary(self, adv_db):
        d = _json(_invoke(["advisory", "holdings"]))
        assert d["summary"]["active_count"] == 2
        assert d["summary"]["avg_pnl_pct"] == pytest.approx((5.6 + -2.1) / 2)

    def test_pending_exits_separate(self, adv_db):
        d = _json(_invoke(["advisory", "holdings"]))
        assert len(d["pending_exits"]) == 1
        assert d["pending_exits"][0]["code"] == "000858"
        assert d["pending_exits"][0]["reason_zh"] == "触及止盈"

    def test_alert_near_stop_loss(self, adv_db):
        """茅台 last_price 1420 距 stop_loss 1380 仅 2.9% → alert_zh 提示"""
        d = _json(_invoke(["advisory", "holdings"]))
        p = next(p for p in d["positions"] if p["code"] == "600519")
        assert p["alert_zh"]  # 接近参考卖出线提示

    def test_schema_fields(self, adv_db):
        d = _json(_invoke(["advisory", "holdings"]))
        assert d["schema_version"] == 1
        assert d["command"] == "advisory holdings"
        assert "data_asof" in d
        assert "positions" in d and "summary" in d and "pending_exits" in d

    def test_empty_portfolio_honest(self, adv_db):
        """无持仓 → ok:true + 空列表 + 中文说明, 不报错"""
        conn = sqlite3.connect(adv_db)
        conn.execute("UPDATE stock_pool SET status='exited'")
        conn.commit()
        conn.close()
        d = _json(_invoke(["advisory", "holdings"]))
        assert d["ok"] is True
        assert d["positions"] == []
        assert d["summary"]["active_count"] == 0

    def test_human_output_chinese(self, adv_db):
        r = _invoke(["advisory", "holdings"], json_mode=False)
        assert r.exit_code == 0
        assert "推荐跟踪" in r.output


class TestPolishNameJoin:
    """① name join 修复 — stock_pool 部分股 name 存的是代码"""

    def test_name_from_stock_info_when_pool_name_is_code(self, adv_db):
        _add_position(adv_db, "000002", "000002", 10.0, 10.5, "2026-08-20",
                      stop=9.5, qty=1000, pnl=5.0, days=4)
        d = _json(_invoke(["advisory", "holdings"]))
        p = next(p for p in d["positions"] if p["code"] == "000002")
        assert p["name"] == "万科A", "pool.name 是代码时应从 stock_info 取真名"

    def test_name_fallback_to_pool_when_no_stock_info(self, adv_db):
        _add_position(adv_db, "920001", "北交所测试股", 5.0, 5.2, "2026-08-20",
                      stop=4.5, qty=1000, pnl=4.0, days=4)
        d = _json(_invoke(["advisory", "holdings"]))
        p = next(p for p in d["positions"] if p["code"] == "920001")
        assert p["name"] == "北交所测试股", "stock_info 无该股时回退 pool.name"

    def test_pending_exit_name_also_joined(self, adv_db):
        conn = sqlite3.connect(adv_db)
        conn.execute(f"INSERT INTO stock_pool ({_POOL_COLS}) VALUES "
                     "('000002','000002','exit_pending',10.0,10.5,1000,5.0,4,"
                     "9.5,NULL,11.5,'测试退出','2026-08-20','2026-08-27')")
        conn.commit()
        conn.close()
        d = _json(_invoke(["advisory", "holdings"]))
        e = next(e for e in d["pending_exits"] if e["code"] == "000002")
        assert e["name"] == "万科A"


class TestPolishEntryDayPrice:
    """② 今日入场无新价 — 禁用昨收/成交价冒充现价, 宁可 NULL 不可假值"""

    def test_entered_today_no_close_honest_null(self, adv_db):
        _add_position(adv_db, "300750", "宁德时代", 100.0, 100.0, TODAY,
                      stop=95.0, qty=100, pnl=0.0, days=0)
        d = _json(_invoke(["advisory", "holdings"]))
        p = next(p for p in d["positions"] if p["code"] == "300750")
        assert p["last_price"] is None, "今日入场无今日收盘价, 不得用成交价冒充现价"
        assert p["unrealized_pnl_pct"] is None
        assert p["note_zh"] == "今日入场,暂无最新收盘价,盈亏待结算"

    def test_entered_today_with_close_uses_kline_close(self, adv_db):
        _add_position(adv_db, "688981", "中芯国际", 50.0, 50.0, TODAY,
                      stop=47.0, qty=200, pnl=0.0, days=0)
        conn = sqlite3.connect(adv_db)
        conn.execute("INSERT INTO daily_klines VALUES ('688981', ?, 51.5)", (TODAY,))
        conn.commit()
        conn.close()
        d = _json(_invoke(["advisory", "holdings"]))
        p = next(p for p in d["positions"] if p["code"] == "688981")
        assert p["last_price"] == 51.5, "有今日收盘价时应用收盘价"
        assert p["unrealized_pnl_pct"] == pytest.approx(3.0)
        assert p["note_zh"] == ""

    def test_entered_today_null_pnl_excluded_from_avg(self, adv_db):
        _add_position(adv_db, "300750", "宁德时代", 100.0, 100.0, TODAY,
                      stop=95.0, qty=100, pnl=0.0, days=0)
        d = _json(_invoke(["advisory", "holdings"]))
        # NULL 盈亏不得拉低平均 (基础两只 5.6 / -2.1)
        assert d["summary"]["avg_pnl_pct"] == pytest.approx((5.6 + -2.1) / 2)

    def test_note_rendered_in_human_output(self, adv_db):
        _add_position(adv_db, "300750", "宁德时代", 100.0, 100.0, TODAY,
                      stop=95.0, qty=100, pnl=0.0, days=0)
        r = _invoke(["advisory", "holdings"], json_mode=False)
        assert "今日入场,暂无最新收盘价,盈亏待结算" in r.output


class TestPolishWording:
    """③ 措辞校准 — F 措辞表: 推荐跟踪 / 参考卖出线 / 验证收益"""

    def test_human_title_recommendation_tracking(self, adv_db):
        r = _invoke(["advisory", "holdings"], json_mode=False)
        assert "推荐跟踪" in r.output
        assert "持仓" not in r.output

    def test_alert_wording_reference_sell_line(self, adv_db):
        d = _json(_invoke(["advisory", "holdings"]))
        p = next(p for p in d["positions"] if p["code"] == "600519")
        assert "参考卖出线" in p["alert_zh"]
        assert "止损位" not in p["alert_zh"]

    def test_pnl_wording_validation_return(self, adv_db):
        r = _invoke(["advisory", "holdings"], json_mode=False)
        assert "验证收益" in r.output
        assert "盈亏" not in r.output

    def test_empty_human_wording(self, adv_db):
        conn = sqlite3.connect(adv_db)
        conn.execute("UPDATE stock_pool SET status='exited'")
        conn.commit()
        conn.close()
        r = _invoke(["advisory", "holdings"], json_mode=False)
        assert "当前无跟踪中的推荐" in r.output

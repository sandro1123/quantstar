# -*- coding: utf-8 -*-
"""#56 alias 扩种卡 — 地理常用词名 RED (TDD, 生产实弹样本)

取证 (2026-09-02, 生产 ro, /tmp/alias_titles_out.txt):
  太平洋 601099: 3/3 全误归 — 东亚及太平洋央行会议(07-23)/东太平洋
    打击贩毒船(08-25)/西北太平洋台风(08-28) — route2 误归率 100%
  大西洋 600558: route2 1/1 误归 — 跨大西洋贸易稳定(07-24);
    colon 路线 1/1 正确 — 兴业银锡：大西洋锡业项目(08-13) 必须保住
机制定性: 「A股简称撞地理常用词」= COMMON_NOUN_BLOCKLIST 族
  (机器人/太阳能先例) — 只禁 route2 子串, colon 路线全词表保留。
  非「更长实体嵌套」= NEGATIVE_ALIASES 族 (地理短语变体 东/西北/跨/
  亚太…不可枚举, 挂负向别名表即 brittle, 零频次短语不入表)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest

from common.database import Database
from data_layer.news_attribution import NewsAttributor


@pytest.fixture
def attr():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS stock_info (code TEXT, name TEXT)")
    conn.executemany(
        "INSERT INTO stock_info (code, name) VALUES (?, ?)",
        [("601099", "太平洋"), ("600558", "大西洋"), ("000426", "兴业银锡")],
    )
    conn.commit()
    a = NewsAttributor(db)
    a.load_vocab()
    yield a
    db.close()


# ---------- RED: 生产实弹地理误归样本 → 必须拒归属 ----------

def test_geo_taipingyang_central_bank_meeting_rejected(attr):
    """生产 2026-07-23 实弹: 「东亚及太平洋中央银行行长会议」地理短语
    曾误归 601099 vocab_title — 扩种后必须 None。"""
    res = attr.attribute(
        "中国人民银行副行长、国家外汇管理局局长朱鹤新出席东亚及太平洋"
        "中央银行行长会议组织第31届行长会")
    assert res is None


def test_geo_taipingyang_east_pacific_rejected(attr):
    """生产 2026-08-25 实弹: 「东太平洋」打击贩毒船 — 地理用法。"""
    res = attr.attribute("美军称在东太平洋打击“贩毒船”致两人死亡")
    assert res is None


def test_geo_taipingyang_typhoon_rejected(attr):
    """生产 2026-08-28 实弹: 「西北太平洋」台风生成 — 地理用法。"""
    res = attr.attribute("今年第23号台风“班朗”在西北太平洋洋面上生成")
    assert res is None


def test_geo_daxiyang_transatlantic_rejected(attr):
    """生产 2026-07-24 实弹: 「跨大西洋」贸易稳定 — route2 误归 600558。"""
    res = attr.attribute(
        "美国抨击欧盟重罚谷歌并向空客提供贷款 称有损跨大西洋贸易稳定")
    assert res is None


# ---------- 守卫: colon 路线必须存活 (blocklist 不作用冒号路线) ----------

def test_colon_route_survives_geo_name_company(attr):
    """colon 前缀精确匹配走全词表不受 blocklist 影响 — 「太平洋：」
    仍是该公司 (blocklist 只禁 route2 子串模糊配, 冒号是精确配)。"""
    res = attr.attribute("太平洋：最新策略研报观点")
    assert res is not None and res.code == "601099" and res.method == "vocab_colon"


def test_colon_daxiyang_project_real_case_survives(attr):
    """生产 2026-08-13 实弹正例 (colon 1/1 正确): 兴业银锡大西洋锡业
    项目 → 000426。扩种大西洋绝不能误杀此归属。"""
    res = attr.attribute("兴业银锡：大西洋锡业项目预计于四季度正式开工建设")
    assert res is not None and res.code == "000426"

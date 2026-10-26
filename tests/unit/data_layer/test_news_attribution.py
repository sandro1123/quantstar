"""P2-19 Phase 0 — 新闻词表归属单元测试 (宁漏勿错语义)"""
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
        [("600100", "同方股份"), ("002967", "广电计量"), ("300926", "华安鑫创"),
         ("600101", "明星电力"), ("000001", "平安银行"), ("300024", "机器人"),
         ("002594", "比亚迪")],
    )
    conn.commit()
    a = NewsAttributor(db)
    a.load_vocab()
    yield a
    db.close()


def test_colon_format_highest_priority(attr):
    res = attr.attribute("广电计量：上半年净利润同比增长20%", "无关摘要")
    assert res is not None
    assert res.code == "002967"
    assert res.method == "vocab_colon"


def test_colon_ascii_separator(attr):
    res = attr.attribute("广电计量: 中标某项目")
    assert res is not None and res.method == "vocab_colon"


def test_colon_prefix_not_in_vocab_falls_through(attr):
    # 冒号前缀不在词表 → 不硬配, 走子串路线
    res = attr.attribute("某未知公司：广电计量中标项目")
    assert res is not None and res.code == "002967" and res.method == "vocab_title"


def test_title_substring_single(attr):
    res = attr.attribute("华安鑫创获机构调研")
    assert res is not None and res.code == "300926" and res.method == "vocab_title"


def test_summary_fallback_removed_no_attribution(attr):
    # #56 案一: 路线3 (vocab_summary) 已砍 — 改判 None。title 主语缺失时
    # summary 单命中 = 顺带提及错归属 (生产 353/353 实测), 宁漏勿错。
    # 详见 vault 分层讨论/卡56-summary路线误报摸底报告-F-20260831.md
    res = attr.attribute("机构调研动态", "华安鑫创获多家机构调研")
    assert res is None

def test_title_no_subject_summary_single_hit_no_attribution(attr):
    # #56 回归锁: title 主语非词表公司 (大盘/别家/海外主体新闻), summary
    # 顺带提及上市公司全名 → 不归属 (原 vocab_summary 路线 100% 语境错归属)
    res = attr.attribute("港股开盘：主要指数高开，美团涨超3%", "新能源车产业链活跃，比亚迪获北上资金加仓")
    assert res is None


def test_ambiguity_returns_none(attr):
    # 两个不同 code 命中 → 宁漏勿错
    res = attr.attribute("同方股份与明星电力合作")
    assert res is None


def test_no_match_returns_none(attr):
    assert attr.attribute("尼泊尔山洪死亡人数升至177人") is None


def test_empty_inputs(attr):
    assert attr.attribute("", "") is None
    assert attr.attribute(None or "", None or "") is None


def test_common_noun_blocked_in_substring(attr):
    # "机器人"是 300024 但泛用名词 — 子串路线不配
    assert attr.attribute("2026世界机器人大会备受关注") is None
    assert attr.attribute("机构看好人形机器人产业成长空间") is None


def test_common_noun_colon_still_allowed(attr):
    # 冒号格式是公司新闻惯例, 保留
    res = attr.attribute("机器人：上半年净利润同比增长")
    assert res is not None and res.code == "300024" and res.method == "vocab_colon"


def test_duplicate_company_name_excluded():
    # 重名公司 (不同 code 同名) 从词表剔除, 防错配
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS stock_info (code TEXT, name TEXT)")
    conn.executemany("INSERT INTO stock_info (code, name) VALUES (?, ?)",
                     [("600000", "重名公司"), ("000002", "重名公司")])
    conn.commit()
    a = NewsAttributor(db)
    n = a.load_vocab()
    assert n == 0
    assert a.attribute("重名公司发布公告") is None
    db.close()


def test_collector_persist_wires_attribution():
    """P2-19 Phase 0: NewsCollector._persist 对无 code 条目走词表归属"""
    from data_layer.news_collector import NewsCollector
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS stock_info (code TEXT, name TEXT)")
    conn.execute("INSERT INTO stock_info (code, name) VALUES ('002967', '广电计量')")
    conn.commit()
    nc = NewsCollector(db=db)
    nc._persist([
        {"date": "2026-08-27", "title": "广电计量：上半年净利增长", "source": "东方财富",
         "sentiment": "bullish", "sentiment_score": 70.0, "summary": ""},
        {"date": "2026-08-27", "title": "尼泊尔山洪死亡人数升至177人", "source": "东方财富",
         "sentiment": "neutral", "sentiment_score": 50.0, "summary": ""},
    ])
    rows = conn.execute(
        "SELECT code, attribution_method FROM stock_news ORDER BY rowid"
    ).fetchall()
    assert rows[0]["code"] == "002967"
    assert rows[0]["attribution_method"] == "vocab_colon"
    assert rows[1]["code"] == ""  # 未命中留空, 不造假
    assert rows[1]["attribution_method"] is None
    db.close()

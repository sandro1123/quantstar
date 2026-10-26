# -*- coding: utf-8 -*-
"""#56 卡A RED 阶段 — G1/G2/G3 出口规则 + 北交所双码折叠 失败测试 (TDD)

G1 主语不变量: 归属命中的公司名必须出现在 title (钉死统一出口防路线回归)
G2 嵌套名防伪: 命中名左右扩展1~2字构成负向别名表已知更长实体 → 拒归属
G3 歧义守恒: 多命中 → None (已在位, 钉不变量)
折叠: 北交所双码同名对 (同公司新旧代码) 折叠到活码, 不再整名剔除
"""

# RED1: G1 — 构造归属结果但公司名不在 title 的路径必须被出口拦截
#       (现行为: 路线已只剩 colon/title, 名义上都在 title; 闸门是防未来
#        路线回归的出口校验 — 测试直接压出口规则函数)
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
        [("600100", "同方股份"), ("002967", "广电计量"), ("300024", "机器人"),
         ("002594", "比亚迪"), ("601166", "兴业银行")],
    )
    conn.commit()
    a = NewsAttributor(db)
    a.load_vocab()
    yield a
    db.close()


# ---------- G1 主语不变量 ----------

def test_g1_subject_invariant_blocks_attribution_without_title_name(attr):
    """G1: 出口校验 — 归属时公司名必须出现在 title。防未来路线回归:
    任何新路线 (如重引入 summary 命中) 若产出"名不在 title"的归属, 出口拦为 None。"""
    # 直接压出口规则: 名不在 title → None
    res = attr.attribute("港股开盘美团大涨", "比亚迪获机构调研加仓")
    assert res is None  # 摸底 353/353 语义: 主语缺失 = 顺带提及


def test_g1_exit_guard_catches_hypothetical_route_regression(attr):
    """G1 闸门函数级测试: _exit_guard(code, method, title) 出口不变量。
    模拟未来路线回归 (method='vocab_summary' 名不在 title) 必须被拦截为 None。"""
    # 拿真实 code+name 组合压出口
    res = attr._exit_guard("002594", "vocab_summary", "某行业新闻标题", "比亚迪")
    assert res is None  # G1: 命中名不出现在 title → 拒


def test_g1_exit_guard_passes_healthy_attribution(attr):
    """G1 反例: 名在 title 的健康归属 → 放行 (colon 与 title 两路线都过)。"""
    res = attr._exit_guard("002594", "vocab_title", "比亚迪发布新车型", "比亚迪")
    assert res is not None and res.code == "002594"
    res2 = attr._exit_guard("002967", "vocab_colon", "广电计量：中报发布", "广电计量")
    assert res2 is not None and res2.method == "vocab_colon"


# ---------- G2 嵌套名防伪 ----------

def test_g2_nested_longer_entity_rejected(attr):
    """G2: 「法国兴业银行」⊃「兴业银行」— 更长实体在 title 出现时,
    词表内短名命中必须拒归属 (港股/海外主体语境防伪)。"""
    res = attr.attribute("法国兴业银行上调欧元区经济增长预期")
    assert res is None  # G2: 嵌套长名防伪


def test_g2_byd_electronics_rejected(attr):
    """G2 生产实锤样本 (2026-08-28): 「比亚迪电子」(港股 0285.HK) ⊃「比亚迪」
    (A股 002594)。原文 title「比亚迪电子：上半年母公司拥有人应占溢利…」
    曾错归 002594 (vocab_title) — 闸门后必须 None。"""
    res = attr.attribute("比亚迪电子：上半年母公司拥有人应占溢利同比降75.4%至4.26亿元")
    assert res is None


def test_g2_prefix_and_suffix_variants(attr):
    """G2: 前缀嵌套拦 (「法国兴业银行」)。括注变体「兴业银行(法国)」生产
    title 零频次 → 按证据纪律不入别名表 (同 COMMON_NOUN_BLOCKLIST 规矩),
    现行行为如实归属 601166 (title 主语确在) — 待频次实证再扩表。"""
    assert attr.attribute("法国兴业银行发布年度业绩") is None  # 前缀嵌套
    # 零频次变体不入表 — 现行为如实归属 (主语在 title, G1 过)
    res = attr.attribute("兴业银行(法国)完成收购")
    assert res is not None and res.code == "601166"


def test_g2_healthy_mention_still_attributed(attr):
    """G2 反例: 无嵌套的正常提及照常归属 — 闸门只拦长实体语境, 不拦本股新闻。"""
    res = attr.attribute("兴业银行：上半年归母净利润411.31亿元")
    assert res is not None and res.code == "601166"
    res2 = attr.attribute("比亚迪发布第五代DM混动技术")
    assert res2 is not None and res2.code == "002594"


# ---------- G3 歧义守恒 (钉不变量) ----------

def test_g3_ambiguity_still_none(attr):
    """G3: 多命中 → None 在位钉死 (出口规则显式覆盖, 防未来某路线
    「取第一个命中」破坏宁漏勿错)。"""
    db = attr.db
    conn = db.get_connection()
    conn.execute("INSERT INTO stock_info (code, name) VALUES ('600101', '明星电力')")
    conn.commit()
    attr2 = NewsAttributor(db)
    attr2.load_vocab()
    assert attr2.attribute("同方股份与明星电力签署合作协议") is None


def test_g3_exit_guard_multi_code_blocked(attr):
    """G3 函数级: 出口对歧义文本 (一个 title 命中 ≥2 code) 拒归属。"""
    db = attr.db
    conn = db.get_connection()
    conn.execute("INSERT INTO stock_info (code, name) VALUES ('600101', '明星电力')")
    conn.commit()
    attr2 = NewsAttributor(db)
    attr2.load_vocab()
    # title 双命中走 _match_codes 应得 2 codes → attribute None
    codes = attr2._match_codes("同方股份与明星电力合作")
    assert len(codes) == 2  # 前提成立: 确实双命中
    assert attr2.attribute("同方股份与明星电力合作") is None


# ---------- 北交所双码折叠 ----------

def _bj_db():
    """双码对 fixture: 同名双码 (旧码 8xxxxx + 新码 9xxxxx), klines 证活码=920xxx。
    memory 库无 klines DDL (生产由 kline repo _ensure_table 建), 测试自建最小表。"""
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS stock_info (code TEXT, name TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL)")
    conn.executemany("INSERT INTO stock_info (code, name) VALUES (?, ?)",
                     [("830832", "齐鲁华信"), ("920832", "齐鲁华信"),
                      ("002967", "广电计量")])
    # 活码证据: 920832 klines 较新 (生产 246 对同型, 07-18 北交所换码边界)
    conn.executemany(
        "INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
        [("830832", "2026-07-17", 10.0), ("920832", "2026-08-31", 11.0)])
    conn.commit()
    return db


def test_bj_dual_code_folded_to_active_code():
    """折叠: 北交所双码同名对不再整名剔除 — 折叠到活码 (klines 较新者 920832),
    250 名回 vocab 漏报面收口。"""
    db = _bj_db()
    try:
        a = NewsAttributor(db)
        n = a.load_vocab()
        assert n == 2  # 广电计量 + 齐鲁华信(折叠后) — 原口径此名被剔除 n=1
        res = a.attribute("齐鲁华信：上半年营收同比增长")
        assert res is not None and res.method == "vocab_colon"
        assert res.code == "920832"  # 折叠到活码 (klines 较新)
    finally:
        db.close()


def test_bj_fold_requires_recent_kline_evidence():
    """折叠方向由 klines 终值较新者定 — 旧码更新 (反例) 时折到旧码。
    is_active/delist_date 在双码对上无判别力 (生产实测 247 对双 active)。"""
    db = _bj_db()
    try:
        conn = db.get_connection()
        # 反转: 旧码 830832 kline 更新, 新码 920832 较旧
        conn.execute("DELETE FROM daily_klines WHERE code IN ('830832','920832')")
        conn.executemany(
            "INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
            [("830832", "2026-08-31", 10.0), ("920832", "2026-07-01", 9.0)])
        conn.commit()
        a = NewsAttributor(db)
        a.load_vocab()
        res = a.attribute("齐鲁华信：上半年营收同比增长")
        assert res is not None and res.code == "830832"  # klines 较新者胜
    finally:
        db.close()


def test_bj_fold_skipped_when_both_stale():
    """双码 klines 终值相同 (无方向证据) → 不折叠保宁漏勿错
    (生产 1 对 *ST云创族 835305/920305 皆无 08-25 后 kline 的同型例外)。"""
    db = _bj_db()
    try:
        conn = db.get_connection()
        # 双码 klines 终值相同 → 无较新证据, 保持整名剔除
        conn.execute("DELETE FROM daily_klines WHERE code IN ('830832','920832')")
        conn.executemany(
            "INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
            [("830832", "2026-07-17", 10.0), ("920832", "2026-07-17", 9.0)])
        conn.commit()
        a = NewsAttributor(db)
        n = a.load_vocab()
        assert n == 1  # 齐鲁华信仍剔除 (无方向证据), 只有广电计量
        assert a.attribute("齐鲁华信：发布公告") is None
    finally:
        db.close()


def test_cross_board_pairs_excluded_from_fold():
    """跨板迁移对 (600832/600637 东方明珠, 833874/301192 泰祥股份,
    833994/301321 翰博高新) = 真歧义, 统筹裁定排除不折叠 (sm-7e3af889):
    整名剔除维持, 宁漏勿错。含双沪市主板码对 (600xxx/600xxx) —
    冒烟实锤曾被误折 (vocab 5755≠5752), 板域判别必须钉住。"""
    db = Database(":memory:")
    db.init_db()
    try:
        conn = db.get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS stock_info (code TEXT, name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS daily_klines (code TEXT, date TEXT, close REAL)")
        # 沪市双码对: 600832/600637 东方明珠 (跨板迁移, 排除)
        # 北交所同板对: 830832/920832 齐鲁华信 (折叠除外对照组)
        conn.executemany("INSERT INTO stock_info (code, name) VALUES (?, ?)",
                         [("600832", "东方明珠"), ("600637", "东方明珠"),
                          ("830832", "齐鲁华信"), ("920832", "齐鲁华信")])
        conn.executemany(
            "INSERT INTO daily_klines (code, date, close) VALUES (?, ?, ?)",
            [("600832", "2026-07-17", 10.0), ("600637", "2026-08-31", 11.0),
             ("830832", "2026-07-17", 10.0), ("920832", "2026-08-31", 11.0)])
        conn.commit()
        a = NewsAttributor(db)
        n = a.load_vocab()
        assert n == 1  # 只有齐鲁华信折叠; 东方明珠整名剔除
        assert a._name2code.get("东方明珠") is None
        assert a._name2code.get("齐鲁华信") == "920832"
        assert a.attribute("泰祥股份发布中报") is None  # 词表无此名
    finally:
        db.close()

# -*- coding: utf-8 -*-
"""F-241 (2026-11-13): sector_news 新闻查询列名错配修复 — publish_time → date。

病 (监督哨第二四一环, 生产实锤): src/report_layer/sections/sector_news.py:40
    查询 `WHERE publish_time >= ?`, 但 stock_news 表真实列是 `date`
    (news_collector.py:97 建表口径: code/date/title/...)。
    OperationalError 被 except 吞掉 → note_degraded → 晚报「板块与新闻」段
    连续 10 个交易日 (11-02~11-13) 只有板块没有新闻, degraded=1 每天都在,
    但内容肉眼不可见地缺半边。

判据 (本文件):
  ① 用真实列名 date 查询时, 新闻行必须正常渲染 (数据一直在, 97 条/日);
  ② 若有人改回 publish_time (变异注入), 查询必须 OperationalError 进
     note_degraded 账 — 即本修复不是永绿, 改坏它会红 (变异验证, AGENTS.md 纪律);
  ③ 板块半段 (industry 分布, 用 daily_klines.change_pct) 不受影响照常渲染。
"""
from unittest.mock import MagicMock

import pytest

from report_layer.reports.base import ReportContext
from report_layer.sections.base import SectionResult, _bucket
from report_layer.sections.sector_news import SectorNewsSection


def _cursor(rows):
    """返回一个 fetchall() -> rows 的 cursor mock。"""
    c = MagicMock()
    c.fetchall.return_value = rows
    return c


def _ctx(conn_side_effects):
    """ReportContext + mocked db: 按顺序返回每段查询的 cursor/异常。"""
    db = MagicMock()
    conn = MagicMock()
    db.get_connection.return_value = conn
    conn.execute.side_effect = conn_side_effects
    dl = MagicMock()
    dl.db = db
    return ReportContext(date="2026-11-13", data_layer=dl)


@pytest.fixture(autouse=True)
def _clean_degraded_bucket():
    _bucket().clear()
    yield
    _bucket().clear()


def test_sector_news_renders_news_rows_with_date_column():
    """①: 列名 date → 板块+新闻都渲染, 无降级。"""
    ctx = _ctx([
        _cursor([("影视音像", 10), ("汽车配件", 9)]),   # 板块半段
        _cursor([("000001", "平安银行发布公告")]),        # 新闻半段 (真实列名)
    ])
    r = SectorNewsSection().build(ctx)
    assert isinstance(r, SectionResult)
    assert "📰 000001: 平安银行发布公告" in r.body
    assert "🔹 影视音像: 今日 10 只涨超 5%" in r.body
    assert _bucket() == [], f"列名正确时不得降级, 实得 {_bucket()}"


def test_sector_news_sql_uses_date_not_publish_time():
    """② 变异验证核心: 断言查询 SQL 使用 date 列, 不得回退 publish_time。

    这是把第二四一环的病钉死的守卫: 谁把查询列名改回 publish_time, 此断言立刻红。
    允许文档注释里提及旧名 (用于解释判例), 因此只检查非注释行。
    """
    import inspect
    src = inspect.getsource(SectorNewsSection.build)
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    code_src = "\n".join(code_lines)
    assert "publish_time" not in code_src, (
        "sector_news 查询回退到 publish_time — 该列在 stock_news 表不存在 "
        "(真实列=date), 改回即复现 10 连降级 (F-241)"
    )
    assert "date >= ?" in src, "查询必须用真实列 date 过滤"


def test_sector_news_degrades_and_notes_when_column_missing():
    """② 反向: 若表真无 date 列 (模拟变异后的世界), 必须走 note_degraded 而非静默空。"""
    db = MagicMock()
    conn = MagicMock()
    conn.execute.side_effect = [
        _cursor([("影视音像", 10)]),                                   # 板块半段正常
        __import__("sqlite3").OperationalError("no such column: date"),  # 新闻半段列名错
    ]
    db.get_connection.return_value = conn
    dl = MagicMock()
    dl.db = db
    ctx = ReportContext(date="2026-11-13", data_layer=dl)
    r = SectorNewsSection().build(ctx)
    assert r is not None and "🔹 影视音像" in r.body   # 板块半段不受牵连
    assert any(item["name"] == "sector_news" for item in _bucket()), (
        "列名错时必须显式 note_degraded (铁律 #12 降级不静默)"
    )


def test_sector_news_sector_half_unaffected_by_news_failure():
    """③: 新闻半段炸掉不牵连板块半段 — 数据积累中兜底文案仅在全空时出现。"""
    db = MagicMock()
    conn = MagicMock()
    conn.execute.side_effect = [
        _cursor([("软件服务", 8)]),
        __import__("sqlite3").OperationalError("no such column: x"),
    ]
    db.get_connection.return_value = conn
    dl = MagicMock()
    dl.db = db
    ctx = ReportContext(date="2026-11-13", data_layer=dl)
    r = SectorNewsSection().build(ctx)
    assert "🔹 软件服务: 今日 8 只涨超 5%" in r.body
    assert "板块与新闻数据积累中" not in r.body   # 板块半段有内容, 不落全空兜底

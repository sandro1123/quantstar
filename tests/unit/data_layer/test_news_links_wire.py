"""AG-NEWS links 通电微卡 (E席, 统筹 sm-4cff88c5 甲案 GO): NewsPipeline.run 尾
piggyback 双调 — backfill_news_stock_links + second_pass_title_scan.

发现B升级定谳 (E席读数 2026-09-15): links 整表三面全断 (写面 INSERT 仅
news_stock_links.py 两处零 import 调用 / 读面 src 零消费者 / 09-12 后 3422
恒定) = 「建了没通电」完整形态 P2。甲案 = 管道尾哨兵式双调:
  - backfill: 新归属行 (vocab_*/derived/per_stock_em) 增量灌入 links, 幂等;
  - second_pass: per_stock_em 行标题全名扫描多股关联补全;
  - 双调各裹哨兵 try (06fc3b3 decay 哨兵先例): 失败只 warning 进 metadata,
    不进 errors 不改 status — 增强面故障不得杀主采集管道;
  - 对数进 metadata: links_backfilled / links_title_scan_2nd 两键。

红-绿: 撤回 piggyback 段时本文件必须失败。
"""
import sqlite3

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.news import NewsPipeline


class _Db:
    """最小 db 桩: 只提供 get_connection() (NewsCollector 只用这一个面)。"""

    def __init__(self, conn):
        self._conn = conn

    def get_connection(self):
        return self._conn


def _make_db(news_rows, names=None):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE stock_news (code TEXT, date TEXT, title TEXT, source TEXT,
            sentiment TEXT, sentiment_score REAL, summary TEXT, attribution_method TEXT,
            PRIMARY KEY (code, date, title))
    """)
    conn.execute("""
        CREATE TABLE news_stock_links (
            news_rowid INTEGER, code TEXT, match_method TEXT,
            PRIMARY KEY (news_rowid, code))
    """)
    conn.execute("""
        CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT, market TEXT,
            list_date TEXT, is_active INTEGER)
    """)
    for r in news_rows:
        conn.execute(
            "INSERT INTO stock_news (code, date, title, source, sentiment,"
            " sentiment_score, summary, attribution_method)"
            " VALUES (?, ?, ?, '测试源', 'neutral', 50.0, '', ?)",
            (r[0], r[1], r[2], r[3]),
        )
    for code, name in (names or []):
        conn.execute("INSERT INTO stock_info (code, name) VALUES (?, ?)", (code, name))
    conn.commit()
    return conn


class TestNewsPipelineLinksPiggyback:
    """甲案主行为: run 尾双调, links 表与 metadata 双落痕。"""

    def test_backfill_writes_links_for_attributed_rows(self, monkeypatch):
        """管道跑完 (collect 被 mock 零采集), 既有归属行 (vocab_colon) 由
        run 尾 backfill 补入 links — 写面通电的铁证。"""
        conn = _make_db([
            ("600000", "2026-09-15", "浦发银行：回购股份公告", "vocab_colon"),
        ])
        pipe = NewsPipeline(db=_Db(conn))
        monkeypatch.setattr(
            type(pipe), "run", NewsPipeline.run, raising=True)  # no-op guard
        # collect 零采集: 主管道无新行也要跑 backfill 增量
        import data_layer.pipelines.news as newsmod
        monkeypatch.setattr(
            newsmod.NewsCollector, "collect", lambda self, date, codes=None: 0)
        res = pipe.run(PipelineInput(date="2026-09-15"))
        n = conn.execute(
            "SELECT COUNT(*) FROM news_stock_links WHERE code='600000'"
            " AND match_method='vocab_colon'").fetchone()[0]
        assert n == 1, (
            f"run 尾 backfill 未通电: links 应为 1 实为 {n} — "
            "写面断供 (发现B) 复发")

    def test_second_pass_links_multi_stock_titles(self, monkeypatch):
        """per_stock_em 多股名行由 run 尾 second_pass 补关联: 「紫金矿业、
        洛阳钼业」资金榜类标题把两只都进 links (title_scan_2nd 法)。"""
        conn = _make_db([
            ("601899", "2026-09-15", "紫金矿业、洛阳钼业资金榜双雄", "per_stock_em"),
        ], names=[("601899", "紫金矿业"), ("603993", "洛阳钼业")])
        pipe = NewsPipeline(db=_Db(conn))
        import data_layer.pipelines.news as newsmod
        monkeypatch.setattr(
            newsmod.NewsCollector, "collect", lambda self, date, codes=None: 0)
        res = pipe.run(PipelineInput(date="2026-09-15"))
        # 本股 (拉取目标) 关联由 backfill 承载 (per_stock_em 法, 1:1);
        # second_pass 只补标题里出现的**其它股** (title_scan_2nd 法) —
        # 双法各司其职, 断言分开数 (夹具初版误把本股也计入 2nd 法)。
        by_method = dict(conn.execute(
            "SELECT match_method, COUNT(*) FROM news_stock_links"
            " GROUP BY match_method").fetchall())
        assert by_method.get("per_stock_em") == 1, (
            f"backfill 未承载本股关联: {by_method}")
        assert by_method.get("title_scan_2nd") == 1, (
            f"second_pass 未补其它股关联: {by_method}")
        codes = sorted(r[0] for r in conn.execute(
            "SELECT code FROM news_stock_links"))
        assert codes == ["601899", "603993"], (
            f"多股关联不完整: {codes}")

    def test_metadata_carries_link_counts(self, monkeypatch):
        """对数留痕 (纪律③): metadata 必带 links_backfilled /
        links_title_scan_2nd 两键 — 读数岗可观测。"""
        conn = _make_db([
            ("600000", "2026-09-15", "浦发银行：回购公告", "vocab_colon"),
        ])
        pipe = NewsPipeline(db=_Db(conn))
        import data_layer.pipelines.news as newsmod
        monkeypatch.setattr(
            newsmod.NewsCollector, "collect", lambda self, date, codes=None: 0)
        res = pipe.run(PipelineInput(date="2026-09-15"))
        md = res.metadata or {}
        assert "links_backfilled" in md, (
            f"metadata 缺 links_backfilled: {sorted(md)}")
        assert "links_title_scan_2nd" in md, (
            f"metadata 缺 links_title_scan_2nd: {sorted(md)}")

    def test_sentinel_never_kills_main_pipeline(self, monkeypatch):
        """哨兵纪律: backfill 抛异常不杀主管道 — status 仍 success,
        病只进 metadata warning, 不进 errors。"""
        conn = _make_db([
            ("600000", "2026-09-15", "浦发银行：回购公告", "vocab_colon"),
        ])
        pipe = NewsPipeline(db=_Db(conn))
        import data_layer.pipelines.news as newsmod
        monkeypatch.setattr(
            newsmod.NewsCollector, "collect", lambda self, date, codes=None: 0)
        # from-import 绑定判例: news.py 直连绑定 backfill 名, patch 须打
        # 消费模块属性 (打源模块属性不生效 — AG-NEWS mock 同族第三案)。
        def _boom(conn):
            raise RuntimeError("backfill 炸了")
        monkeypatch.setattr(newsmod, "backfill_news_stock_links", _boom)
        res = pipe.run(PipelineInput(date="2026-09-15"))
        assert res.status == "success", (
            f"哨兵失效: backfill 异常杀了主管道 status={res.status} "
            f"errors={res.errors}")
        assert res.errors == [], f"errors 应空: {res.errors}"
        md = res.metadata or {}
        warn = str(md.get("links_warning", "")) + str(md.get("links_warnings", ""))
        assert "backfill" in warn, f"哨兵失败无留痕: {md}"

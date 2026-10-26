"""AG-NEWS ④⑤ 回归测试 — RED 先行 (对未施工面)。

覆盖:
  A. NewsCollector._fetch_per_stock_em — akshare stock_news_em 个股腿 (mock akshare)
  B. NewsCollector.collect 增量面 — per-stock 腿合入主管道
  C. 新闻分级 classify_news_tier — 公告>异动>题材>噪声 四桶
  D. NewsIntelExpert (expert_news_intel) — 第七专家 LLM 腿+规则腿降级
  E. ALL_EXPERTS 注册面 — 七专家
  F. news_stock_links 迁移 (apply_news_stock_links_migration) — 建表幂等
  G. link_news_stocks 真写后回填 links 表 (backfill_news_stock_links)
"""
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest


# ---------- A/B: 个股新闻腿 ----------

class TestPerStockNewsLeg:
    def _collector(self, db):
        from data_layer.news_collector import NewsCollector
        return NewsCollector(db=db)

    def test_fetch_per_stock_em_maps_fields(self):
        """stock_news_em 六列映射: 关键词→code, 新闻标题→title, 新闻内容→summary,
        发布时间→date, 文章来源→source。"""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        class _DB:
            def get_connection(self):
                return conn

        c = self._collector(_DB())
        fake_akshare = MagicMock()
        fake_akshare.stock_news_em.return_value = MagicMock(iterrows=lambda: iter([
            (0, {"关键词": "300409", "新闻标题": "道明光学三季报预增",
                 "新闻内容": "公司预计净利润同比增长",
                 "发布时间": "2026-09-10 10:00:00", "文章来源": "证券时报",
                 "新闻链接": "http://x"}),
        ]))
        with patch.dict("sys.modules", {"akshare": fake_akshare}):
            items = c._fetch_per_stock_em(["300409"])
        assert len(items) == 1
        it = items[0]
        assert it["code"] == "300409"
        assert it["title"] == "道明光学三季报预增"
        assert it["source"] == "证券时报"
        assert it["date"].startswith("2026-09-10")
        assert it["attribution_method"] == "per_stock_em"

    def test_fetch_per_stock_em_source_failure_returns_empty(self):
        """单股 429/异常 → 该股跳过不炸腿 (降级记档不停工)。"""
        import sqlite3
        conn = sqlite3.connect(":memory:")

        class _DB:
            def get_connection(self):
                return conn

        c = self._collector(_DB())
        fake_akshare = MagicMock()
        fake_akshare.stock_news_em.side_effect = RuntimeError("429 limit")
        with patch.dict("sys.modules", {"akshare": fake_akshare}):
            items = c._fetch_per_stock_em(["300409", "000001"])
        assert items == []  # 逐股降级, 两股全 skip 不炸腿

    def test_collect_includes_per_stock_leg(self):
        """collect() 主管道必须合入个股腿 (mock 全市场腿为空, 个股腿 2 条)。"""
        import sqlite3
        from data_layer.news_collector import NewsCollector

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_news (
                code TEXT, date TEXT, title TEXT, source TEXT,
                sentiment TEXT, sentiment_score REAL, summary TEXT,
                attribution_method TEXT
            )
        """)
        conn.commit()

        class _DB:
            def get_connection(self):
                return conn

        c = NewsCollector(db=_DB())
        with patch.object(c, "_fetch_global_em", return_value=[]), \
             patch.object(c, "_fetch_caixin", return_value=[]), \
             patch.object(c, "_fetch_cctv", return_value=[]), \
             patch.object(c, "_fetch_per_stock_em", return_value=[
                 {"code": "300409", "title": "道明光学A股回购公告", "summary": "公司拟回购",
                  "date": "2026-09-10", "source": "东财个股",
                  "attribution_method": "per_stock_em"},
             ]):
            n = c.collect("2026-09-10", codes=["300409"])
        assert n >= 1
        row = conn.execute(
            "SELECT code, attribution_method FROM stock_news WHERE title LIKE '%回购公告%'"
        ).fetchone()
        assert row["code"] == "300409"
        assert row["attribution_method"] == "per_stock_em"


# ---------- C: 新闻分级 ----------

class TestNewsTiering:
    def test_announcement_tier_highest(self):
        from data_layer.news_tiering import classify_news_tier
        assert classify_news_tier("道明光学：拟回购股份")["tier"] == 1  # 公告
        assert classify_news_tier("某公司2026年半年度业绩预告")["tier"] == 1

    def test_anomaly_tier_second(self):
        from data_layer.news_tiering import classify_news_tier
        r = classify_news_tier("三连板！XX股涨停原因揭秘")
        assert r["tier"] == 2  # 异动解读

    def test_theme_tier_third(self):
        from data_layer.news_tiering import classify_news_tier
        r = classify_news_tier("半导体板块获政策催化 多股活跃")
        assert r["tier"] == 3  # 题材

    def test_noise_tier_last(self):
        from data_layer.news_tiering import classify_news_tier
        r = classify_news_tier("美股三大指数收涨 鲍威尔讲话")
        assert r["tier"] == 4  # 噪声

    def test_tier_payload_has_label(self):
        from data_layer.news_tiering import classify_news_tier, TIER_LABELS
        r = classify_news_tier("道明光学：拟回购股份")
        assert r["label"] == TIER_LABELS[1]


# ---------- D/E: 第七专家 ----------

class TestNewsIntelExpert:
    def test_registered_in_all_experts(self):
        from agent_layer.debate.experts import ALL_EXPERTS, NewsIntelExpert
        assert NewsIntelExpert in ALL_EXPERTS
        assert len(ALL_EXPERTS) == 7

    def test_expert_name(self):
        from agent_layer.debate.experts import NewsIntelExpert
        assert NewsIntelExpert.name == "news_intel"

    def test_rule_leg_uses_news_tier(self):
        """规则腿: data 带 news_items (分级后) → tier1 公告存在给正分,
        无新闻给中性 0 (诚实降级, 不造假)。"""
        from agent_layer.debate.experts import NewsIntelExpert
        e = NewsIntelExpert()
        rep = e._rule_analyze({
            "news_items": [
                {"title": "拟回购", "tier": 1, "label": "公告"},
                {"title": "涨停解读", "tier": 2, "label": "异动解读"},
            ]
        })
        assert rep.expert_name == "news_intel"
        assert rep.score > 0

    def test_rule_leg_no_news_is_neutral(self):
        from agent_layer.debate.experts import NewsIntelExpert
        e = NewsIntelExpert()
        rep = e._rule_analyze({"news_items": []})
        assert rep.score == 0.0
        assert any("无个股新闻" in p for p in rep.key_points)

    def test_llm_leg_template_name(self):
        """LLM 腿走 expert_news 模板 (③轮初稿)。"""
        from agent_layer.debate import experts as E
        e = E.NewsIntelExpert()
        with patch.object(E, "_llm_expert_opinion", return_value=None) as m:
            e.analyze({"code": "300409", "date": "2026-09-10",
                       "data_summary": "x", "news_items": []})
        m.assert_called_once()
        assert m.call_args.kwargs.get("template_name") == "expert_news"


# ---------- F/G: news_stock_links ----------

class TestNewsStockLinks:
    def _db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_news (
                code TEXT, date TEXT, title TEXT, source TEXT,
                sentiment TEXT, sentiment_score REAL, summary TEXT,
                attribution_method TEXT
            )
        """)
        return conn

    def test_migration_creates_table_idempotent(self):
        from data_layer.news_stock_links import apply_news_stock_links_migration
        conn = self._db()
        apply_news_stock_links_migration(conn)
        apply_news_stock_links_migration(conn)  # 幂等
        cols = [r[1] for r in conn.execute("PRAGMA table_info(news_stock_links)")]
        assert set(cols) >= {"news_rowid", "code", "match_method"}

    def test_backfill_from_derived_rows(self):
        """回填: derived_title_match/vocab_* 行的 (rowid, code, method) 全入 links。"""
        from data_layer.news_stock_links import backfill_news_stock_links
        conn = self._db()
        conn.execute(
            "INSERT INTO stock_news (code, date, title, attribution_method) "
            "VALUES ('688008', '2026-09-10', '回购潮', 'derived_title_match')")
        conn.execute(
            "INSERT INTO stock_news (code, date, title, attribution_method) "
            "VALUES ('300661', '2026-09-10', '公告', 'vocab_colon')")
        conn.execute(
            "INSERT INTO stock_news (code, date, title, attribution_method) "
            "VALUES ('', '2026-09-10', '市场新闻', NULL)")
        conn.commit()
        n = backfill_news_stock_links(conn)
        assert n == 2
        rows = conn.execute("SELECT news_rowid, code, match_method FROM news_stock_links").fetchall()
        methods = {r["match_method"] for r in rows}
        assert methods == {"derived_title_match", "vocab_colon"}

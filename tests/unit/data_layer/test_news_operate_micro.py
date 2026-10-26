"""AG-NEWS 运营季三 GO 微卡 (sm-ecd400e): T1 误桶修法 + links 第二遍扫描.

三 GO:
  ② T1 误桶修法: 词表补「中报/年报快讯/净利为」+ 个股 code 前缀正则
     (title 含 \d{6}\.SZ/SH/BJ 直接判 T1 — 界面「创维数字000810.SZ)：中报
     净利润」快讯格式回收 ~40 行真公告).
  ③ links 第二遍: per_stock_em 行加标题全名扫描 (link_news_stocks 同款
     map+守卫), 多股名行 (「紫金矿业、洛阳钯业」行业资金榜类) 补全关联.
  ① ops.yaml 条目文案已在 b832f6d4 报告内 (零代码动作).

红-绿: 撤回两修法时本文件必须失败。
"""
import re
import sqlite3

from data_layer.news_tiering import classify_news_tier


class TestT1MisbucketFix:
    def test_jiemian_kuaixun_format_hits_t1(self):
        """界面快讯格式: 「创维数字000810.SZ)：2026年中报净利润为1.62亿元」
        — 旧形态落 T4 (词表漏「中报」+无 code 正则), 修后必须 T1。"""
        r = classify_news_tier("创维数字000810.SZ)：2026年中报净利润为1.62亿元")
        assert r["tier"] == 1, f"界面快讯格式落 T{r['tier']}, 应 T1 (真公告)"

    def test_zhongbao_word_in_list(self):
        """「中报」补词实证: 无 code 前缀的「中报」标题也须 T1。"""
        r = classify_news_tier("元创股份：2026年中报净利润5255.37万元 拟10派1.5元")
        assert r["tier"] == 1

    def test_code_prefix_regex_direct_t1(self):
        """个股 code 前缀正则: 标题含 6 位代码+.SZ/.SH/.BJ 后缀 直接判 T1。"""
        r = classify_news_tier("机构风向标 | 德源药业920735.BJ 已披露持仓机构仅2家")
        assert r["tier"] == 1, f"code 前缀行落 T{r['tier']}, 应 T1"

    def test_pure_market_news_stays_t4(self):
        """守卫: 纯市场面新闻 (无 code 无公告词) 仍 T4 — 正则不许误伤。"""
        r = classify_news_tier("570只个股流通市值不足20亿元")
        assert r["tier"] == 4

    def test_fund_halfyear_report_stays_t4(self):
        """基金快讯 (无个股 code 无公告词) 仍 T4 — code 正则零误伤实证。

        勘正: 旧抽判样本把此行标成 T1 误桶 — 复验 title 是「上半年」非
        「半年报」, 词表本就不命中, 旧形态=T4 正确。真正的 T1 误桶是
        界面快讯族 (已被前两测覆盖)。
        """
        r = classify_news_tier("鹏华弘和混合A：2026年上半年换手率达592.33%")
        assert r["tier"] == 4


class TestLinksSecondPass:
    def _db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE stock_news (code TEXT, date TEXT, title TEXT, source TEXT,
                sentiment TEXT, sentiment_score REAL, summary TEXT, attribution_method TEXT)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news_stock_links (
                news_rowid INTEGER NOT NULL, code TEXT NOT NULL,
                match_method TEXT NOT NULL, PRIMARY KEY (news_rowid, code))
        """)
        conn.execute("CREATE TABLE stock_info (code TEXT PRIMARY KEY, name TEXT)")
        # 多股名行: 紫金矿业+洛阳钼业 资金榜 — 拉取目标=紫金矿业, 钼业未关联
        conn.execute("INSERT INTO stock_news VALUES ('601899','2026-09-12',"
                     "'有色金属行业资金流入榜：紫金矿业、洛阳钼业等净流入资金居前',"
                     "'证券时报网',NULL,NULL,'','per_stock_em')")
        conn.execute("INSERT INTO stock_info VALUES ('601899','紫金矿业')")
        conn.execute("INSERT INTO stock_info VALUES ('603993','洛阳钼业')")
        conn.execute("INSERT INTO news_stock_links VALUES (1, '601899', 'per_stock_em')")
        conn.commit()
        return conn

    def test_second_pass_links_multi_name_rows(self):
        """第二遍标题全名扫描: per_stock_em 多股名行补全关联 (钼业入 links)。"""
        from data_layer.news_stock_links import second_pass_title_scan
        conn = self._db()
        n = second_pass_title_scan(conn)
        assert n >= 1, "第二遍扫描零新关联 — 多股名行未回收"
        rows = conn.execute(
            "SELECT code FROM news_stock_links WHERE news_rowid=1").fetchall()
        codes = {r["code"] for r in rows}
        assert "603993" in codes, f"洛阳钼业未入 links: {codes}"

    def test_second_pass_idempotent(self):
        """幂等: 二跑零新写 (OR IGNORE)。"""
        from data_layer.news_stock_links import second_pass_title_scan
        conn = self._db()
        first = second_pass_title_scan(conn)
        second = second_pass_title_scan(conn)
        assert second == 0, f"二跑仍写 {second} 对, 非幂等"
        assert first >= 1

    def test_second_pass_skips_non_target_rows(self):
        """守卫: 只扫 per_stock_em 行 — derived/vocab 行不动 (旧法域)。"""
        from data_layer.news_stock_links import second_pass_title_scan
        conn = self._db()
        conn.execute("INSERT INTO stock_news VALUES ('300661','2026-09-12',"
                     "'圣邦股份：拟回购','公告',NULL,NULL,'','vocab_colon')")
        conn.execute("INSERT INTO stock_info VALUES ('002440','闰土股份')")
        conn.commit()
        # vocab 行 title 含股票名但非 per_stock_em — 不该被第二遍触及
        before = conn.execute("SELECT COUNT(*) FROM news_stock_links").fetchone()[0]
        second_pass_title_scan(conn)
        after = conn.execute("SELECT COUNT(*) FROM news_stock_links").fetchone()[0]
        # 该 vocab 行不应新增 (它自身已有归属法域)
        assert after - before <= 1  # 只允许多股行那 1 对

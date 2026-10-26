"""TDD: F-22b 腿健康面接真实数据源 (_leg_health_face 不再恒返 {}).

病灶: _leg_health_face 恒返 {} (无来源) —— 70 只跟踪票每天全部报「健康面缺失」,
23 天如此。此前被写成「设计性表现、属预期」, 实为「没实现」被说成「有意为之」
(F-22 同族第三例: coverage_ratio 分子恒 0 / record_advice 零调用)。

统筹裁示 (2026-10-22, sm-0efa96db424644fa):
  ① llm_debate 腿判据 = 该票**其 rec_date 当日**有 deep_debate 记录
     (不是「今天有没有辩论」—— 跟踪票是历史推荐票, 用今天判是范畴错误;
      rec_date 当日也无记录 → 标 missing, 是真实缺失, 不是判据问题)
  ② news_intel 腿判据 = 该票**当日有新闻条目**, 不放宽
     (52/53 恒 missing 是实话; 若导致降档过猛那是 R4 权重该调, 不是判据该松)
  ③ 本卡只做健康面产出, apply_rules 接线另立卡

三钉:
  钉1: 有腿状态时正确返回 {leg: 'ok'/'missing'}, 不再恒 {}
  钉2: **健康面不可得**时仍返回 {} 并显式报缺失 (不是静默判健康)
       —— 与「健康面可得但四腿都缺」严格区分 (后者是逐腿事实, 必须报 missing)
  钉3: **不得引入新的假绿** —— 某腿状态拿不到就写 'ok' = 制造新假象, 必须钉死
"""

import sqlite3

from execution_layer.tracking.daily_guard import _leg_health_face


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE recommendation_tracking (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL, rec_date TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'recommended'
    );
    CREATE TABLE daily_klines (code TEXT, date TEXT);
    CREATE TABLE stock_valuation (code TEXT, date TEXT);
    CREATE TABLE stock_moneyflow (code TEXT, date TEXT);
    CREATE TABLE stock_news (code TEXT, date TEXT);
    CREATE TABLE agent_predictions (code TEXT, date TEXT, agent_name TEXT);
    """)
    return conn


ASOF = "2026-10-21"


def _seed_full(conn):
    """该票四腿全有当日数据 → 四腿皆 ok."""
    conn.execute("INSERT INTO recommendation_tracking (code, rec_date, state) VALUES ('600001', '2026-10-21', 'recommended')")
    conn.execute("INSERT INTO daily_klines VALUES ('600001', '2026-10-21')")
    conn.execute("INSERT INTO stock_valuation VALUES ('600001', '2026-10-21')")
    conn.execute("INSERT INTO stock_moneyflow VALUES ('600001', '2026-10-21')")
    conn.execute("INSERT INTO stock_news VALUES ('600001', '2026-10-21')")
    conn.execute("INSERT INTO agent_predictions VALUES ('600001', '2026-10-21', 'deep_debate')")
    conn.commit()


# ───────────────── 钉1: 有腿状态时返回真实映射 ─────────────────
def test_returns_real_leg_status_when_data_present():
    """钉1: 四腿当日数据齐全 → 返回 {leg: 'ok'} 四项, 不再恒 {}."""
    conn = _mk_db()
    _seed_full(conn)

    face = _leg_health_face(conn, "600001", ASOF)

    assert face, "有腿状态时不得返回空字典 (病灶: 恒返 {})"
    assert set(face.keys()) == {"llm_debate", "kline_valuation", "moneyflow", "news_intel"}, \
        f"四腿必须齐全, 实得 {face}"
    assert all(v == "ok" for v in face.values()), f"四腿数据齐全应全 ok, 实得 {face}"
    conn.close()


def test_marks_missing_leg_when_data_absent():
    """钉1 (反向): 某腿当日无数据 → 该腿标 'missing', 不得标 'ok'."""
    conn = _mk_db()
    conn.execute("INSERT INTO recommendation_tracking (code, rec_date, state) VALUES ('600002', '2026-10-21', 'recommended')")
    conn.execute("INSERT INTO daily_klines VALUES ('600002', '2026-10-21')")
    conn.execute("INSERT INTO stock_valuation VALUES ('600002', '2026-10-21')")
    # moneyflow / news / debate 均缺
    conn.commit()

    face = _leg_health_face(conn, "600002", ASOF)

    assert face["kline_valuation"] == "ok", f"有数据腿应 ok, 实得 {face}"
    assert face["moneyflow"] == "missing", f"缺数据腿应 missing, 实得 {face}"
    assert face["news_intel"] == "missing", f"缺数据腿应 missing, 实得 {face}"
    assert face["llm_debate"] == "missing", f"缺数据腿应 missing, 实得 {face}"
    conn.close()


def test_llm_debate_uses_rec_date_not_asof():
    """钉1 (口径钉): llm_debate 按该票**其 rec_date 当日**判, 不按 asof 判.

    跟踪票是历史推荐票 —— 辩论腿只在它被推荐那天跑过。
    该票 rec_date=10-19, 当日(10-19) 有 deep_debate 记录 → llm_debate 应 ok;
    若误按 asof(10-21) 判则必 missing (10-21 没辩论过它)。
    """
    conn = _mk_db()
    conn.execute("INSERT INTO recommendation_tracking (code, rec_date, state) VALUES ('600003', '2026-10-19', 'recommended')")
    conn.execute("INSERT INTO daily_klines VALUES ('600003', '2026-10-21')")
    conn.execute("INSERT INTO stock_valuation VALUES ('600003', '2026-10-21')")
    conn.execute("INSERT INTO agent_predictions VALUES ('600003', '2026-10-19', 'deep_debate')")
    conn.commit()

    face = _leg_health_face(conn, "600003", ASOF)

    assert face["llm_debate"] == "ok", (
        f"llm_debate 须按该票 rec_date 当日判 (10-19 有辩论) → 应 ok, 实得 {face}")
    conn.close()


def test_llm_debate_missing_when_no_debate_on_rec_date():
    """钉1 (口径钉·反向): 该票 rec_date 当日无辩论记录 → llm_debate 标 missing
    (是真实缺失, 不是判据问题)."""
    conn = _mk_db()
    conn.execute("INSERT INTO recommendation_tracking (code, rec_date, state) VALUES ('600004', '2026-10-19', 'recommended')")
    # 只在别的日期有辩论 → 该票 rec_date 当日无
    conn.execute("INSERT INTO agent_predictions VALUES ('600004', '2026-10-21', 'deep_debate')")
    conn.commit()

    face = _leg_health_face(conn, "600004", ASOF)

    assert face["llm_debate"] == "missing", (
        f"rec_date 当日无辩论 → 真实缺失, 应 missing, 实得 {face}")
    conn.close()


def test_news_intel_not_loosened_to_coverage():
    """钉1 (口径钉·news): news_intel 判「该票当日有新闻条目」, 不放宽 ——
    该票当日无新闻条目但库里有别的票有新闻 → 仍判 missing."""
    conn = _mk_db()
    conn.execute("INSERT INTO recommendation_tracking (code, rec_date, state) VALUES ('600005', '2026-10-21', 'recommended')")
    conn.execute("INSERT INTO stock_news VALUES ('999999', '2026-10-21')")  # 别的票有
    conn.commit()

    face = _leg_health_face(conn, "600005", ASOF)

    assert face["news_intel"] == "missing", (
        f"news_intel 判该票当日有无新闻条目, 不按覆盖面放宽; 实得 {face}")
    conn.close()


# ───────────────── 钉2: 健康面「不可得」→ 仍返 {} 并显式报缺失 ─────────────────
def test_returns_empty_when_tracking_row_not_found():
    """钉2: 该票在 recommendation_tracking 里查不到 (健康面整体不可得 ——
    连「这票是谁/它何时被推荐」都不可知) → 返 {} 交调用方显式报缺失,
    不得静默判健康, 也不得用猜测值冒充 'ok'."""
    conn = _mk_db()
    # 故意不插 tracking 行
    conn.commit()

    face = _leg_health_face(conn, "600006", ASOF)

    assert face == {}, f"健康面不可得时须返空字典 (显式报缺失), 实得 {face}"
    conn.close()


def test_returns_empty_when_tables_unavailable():
    """钉2 (边界): 腿表不存在 (查询异常) 且无跟踪行 → 仍返回 {}, 不得抛异常炸主链,
    更不得因异常而把腿写成 'ok'."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE recommendation_tracking (code TEXT, rec_date TEXT, state TEXT)")
    # 故意不建 daily_klines / stock_valuation 等表 → 查询会抛 sqlite3.Error

    face = _leg_health_face(conn, "600007", ASOF)

    assert face == {}, f"表不可用时须返空字典 (显式报缺失), 实得 {face}"
    conn.close()


def test_all_legs_missing_is_reported_as_facts_not_empty():
    """钉2 (边界·关键区分): 健康面**可得**但四腿当日都无数据 → 必须报四腿 missing,
    不得返 {} —— 二者不可混:
      返 {} = 「健康面不可得」(故障态)
      返四腿 missing = 「该票确实当日一条数据都没有」(逐腿事实)
    混同 = 把「数据坏了」说成「这票没数据」, 反向造假象。"""
    conn = _mk_db()
    conn.execute("INSERT INTO recommendation_tracking (code, rec_date, state) VALUES ('600008', '2026-10-21', 'recommended')")
    conn.commit()

    face = _leg_health_face(conn, "600008", ASOF)

    assert face != {}, "有跟踪行 → 健康面可得, 四腿都缺是逐腿事实, 不得返空"
    assert set(face.keys()) == {"llm_debate", "kline_valuation", "moneyflow", "news_intel"}, \
        f"四腿必须齐全, 实得 {face}"
    assert all(v == "missing" for v in face.values()), f"四腿皆无数据应全 missing, 实得 {face}"
    conn.close()


# ───────────────── 钉3: 不得引入新的假绿 ─────────────────
def test_no_leg_written_ok_without_positive_evidence():
    """钉3: 'ok' 只能由「该腿当日数据真实存在」这一正面证据产生 ——
    任何腿取不到状态时, 绝不能写 'ok' (那是制造新的假象)."""
    conn = _mk_db()
    conn.execute("INSERT INTO recommendation_tracking (code, rec_date, state) VALUES ('600009', '2026-10-21', 'recommended')")
    conn.execute("INSERT INTO daily_klines VALUES ('600009', '2026-10-21')")
    conn.execute("INSERT INTO stock_valuation VALUES ('600009', '2026-10-21')")
    # 只填了 2 腿, 其余 2 腿无数据
    conn.commit()

    face = _leg_health_face(conn, "600009", ASOF)

    for leg, status in face.items():
        if leg in ("kline_valuation",):
            assert status == "ok", f"有证据的腿应 ok, 实得 {face}"
        else:
            assert status != "ok", (
                f"腿 {leg} 无任何数据证据却写成 'ok' = 新的假绿; 实得 {face}")
    conn.close()


def test_kline_valuation_requires_both_tables():
    """钉3 (钉死·kline_valuation 口径): kline_valuation 腿要求 **daily_klines 与
    stock_valuation 都在**才算 ok (只到一半 → missing), 不得单表充数."""
    conn = _mk_db()
    conn.execute("INSERT INTO recommendation_tracking (code, rec_date, state) VALUES ('600010', '2026-10-21', 'recommended')")
    conn.execute("INSERT INTO daily_klines VALUES ('600010', '2026-10-21')")
    # 缺 stock_valuation
    conn.commit()

    face = _leg_health_face(conn, "600010", ASOF)

    assert face["kline_valuation"] == "missing", (
        f"仅 daily_klines 有 → 不得判 kline_valuation ok (估值腿缺); 实得 {face}")
    conn.close()

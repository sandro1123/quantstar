import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scripts"))
"""回归测试: 修复晨报选股返回空(暂无推荐)。

根因:
1. _get_stock_data() 的 SQL 强制 s.industry != '', 而 industry 上游源返回空串,
   导致 JOIN 返回 0 行 -> "暂无推荐"。industry 仅用于展示, 不应作为硬过滤。
2. 硬编码权重键(value_discount/roe_quality/...)与 model_predictions 实际键名不对齐。

修复: 改按 model_predictions.blended_score(策略层权威输出, 已含 composite*0.8+ml*0.2) 排序,
且去掉 industry != '' 硬过滤(industry 为空时默认展示为"未知")。

本测试用 monkeypatch 替换 build_morning_brief.Database 为内存库, 不触碰生产库。
"""
import json
import sqlite3
import pytest


@pytest.fixture
def bmb():
    """CF-02: 延迟导入 build_morning_brief, 隔离其 import 期副作用。

    build_morning_brief 是 cron 直驱入口, import 期执行 load_dotenv(.env)
    (注入 CONFIG_ENV=prod 等) + os.chdir(project_root) —— CF-01 定性的有意设计,
    生产脚本零改动。原模块级导入让副作用发生在 collection 期 (早于任何 fixture),
    CONFIG_ENV=prod 漏进 os.environ 污染整个会话 (reset_config 只清单例不清 env),
    与 test_required_secrets 的秘密键泄漏合谋致全量 25F+43E。
    改为 fixture 内导入: 副作用落在 conftest _isolate_env_and_cwd 快照窗口内,
    导入后立即恢复 env/cwd (finally 保证导入失败也恢复), 断言语义不变。
    """
    env_snapshot = dict(os.environ)
    cwd_snapshot = os.getcwd()
    try:
        import build_morning_brief as _bmb
    finally:
        os.environ.clear()
        os.environ.update(env_snapshot)
        os.chdir(cwd_snapshot)
    return _bmb


def _build_seed_db(rows):
    """构造一个与生产 schema 兼容的内存库, 并灌入 model_predictions/factor_daily/stock_info。"""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE factor_daily (date TEXT, code TEXT, factor_scores TEXT)")
    db.execute("CREATE TABLE model_predictions (date TEXT, code TEXT, ml_score REAL, factor_score REAL, blended_score REAL, model_version TEXT)")
    db.execute("CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT)")
    for i, (code, name, industry, blended) in enumerate(rows):
        fs = json.dumps({"pe_ttm": -0.5 + i, "pb": -0.3 + i, "roe": 0.2 + i, "momentum_20d": 0.1 + i})
        db.execute("INSERT INTO factor_daily VALUES (?,?,?)", ("2026-08-13", code, fs))
        db.execute("INSERT INTO model_predictions VALUES (?,?,?,?,?,?)",
                   ("2026-08-13", code, 0.0, blended, blended, "test_v"))
        db.execute("INSERT INTO stock_info VALUES (?,?,?)", (code, name, industry))
    db.commit()
    return db


def test_returns_picks_even_when_industry_empty(monkeypatch, bmb):
    """关键回归: industry 全为空时, 也应返回推荐(industry 不应作为硬过滤)。"""
    db = _build_seed_db([
        ("600001", "甲股", "", 1.5),
        ("600002", "乙股", "", 1.2),
        ("600003", "丙股", "", 0.9),
        ("600004", "丁股", "", 0.6),
        ("600005", "戊股", "", 0.3),
        ("600006", "己股", "", 0.1),
    ])
    monkeypatch.setattr(bmb, "Database", lambda: type("D", (), {"get_connection": lambda self: db})())
    res = bmb._get_stock_data()
    assert len(res) > 0, "industry 全为空时不应返回空列表"
    assert len(res) <= 5


def test_ranks_by_blended_score_desc(monkeypatch, bmb):
    """按 blended_score 降序: 分最高的股票应排在首位。"""
    db = _build_seed_db([
        ("600001", "甲股", "银行", 1.5),
        ("600002", "乙股", "医药", 0.9),
        ("600003", "丙股", "科技", 1.2),
    ])
    monkeypatch.setattr(bmb, "Database", lambda: type("D", (), {"get_connection": lambda self: db})())
    res = bmb._get_stock_data()
    # 按 composite(即 blended_score) 排序
    comps = [d["composite"] for d, _ in res]
    assert comps == sorted(comps, reverse=True), "应按 blended_score 降序"
    assert res[0][0]["code"] == "600001"


def test_industry_defaults_to_unknown(monkeypatch, bmb):
    """industry 为空时展示为 "未知", 不抛错。"""
    db = _build_seed_db([("600001", "甲股", "", 1.0)])
    monkeypatch.setattr(bmb, "Database", lambda: type("D", (), {"get_connection": lambda self: db})())
    res = bmb._get_stock_data()
    assert res and res[0][0]["industry"] in ("", "未知")


def test_no_data_returns_empty(monkeypatch, bmb):
    """无任何因子数据时应返回空列表, 不抛错。"""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE factor_daily (date TEXT, code TEXT, factor_scores TEXT)")
    db.execute("CREATE TABLE model_predictions (date TEXT, code TEXT, ml_score REAL, factor_score REAL, blended_score REAL, model_version TEXT)")
    db.execute("CREATE TABLE stock_info (code TEXT, name TEXT, industry TEXT)")
    monkeypatch.setattr(bmb, "Database", lambda: type("D", (), {"get_connection": lambda self: db})())
    assert bmb._get_stock_data() == []

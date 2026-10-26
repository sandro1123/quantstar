# -*- coding: utf-8 -*-
"""#56 卡B RED 阶段 — audit_attribution 哨兵 (report-only) 失败测试 (TDD)

哨兵三铁律 (统筹 GO sm-79ae07):
  1. 默认关 (enforce=false) — 哨兵宪法: 关态零行为 (不审计/不落 metadata)
  2. 挂 08:00 采集尾 piggyback — 段内 try 不杀主管道 (06fc3b3 先例)
  3. 口径三件落 metadata: 归属分布 / 词表漂移 (size+日环比 delta) /
     G1G2G3 违例计数 — report-only 永不阻断, 零写 (只读探查)

B 复验 A 的规则: G1=落库行 code 反查名不在 title; G2=title 含负向别名
却有归属; G3=title 双命中却有归属 (卡A 闸门若在场会被拒的历史形状)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest

from common.database import Database
from data_layer.news_attribution import NewsAttributor, audit_attribution
from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines import news as news_mod
from data_layer.pipelines.news import NewsPipeline


def _audit_db(extra_rows=()):
    """哨兵口径 fixture: stock_info 两名 + stock_news 当日 4 行
    (3 归属健康 1 未归属)。表结构对齐生产列名。"""
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS stock_info (code TEXT, name TEXT)")
    conn.executemany(
        "INSERT INTO stock_info (code, name) VALUES (?, ?)",
        [("002967", "广电计量"), ("601166", "兴业银行")],
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stock_news (
            code TEXT DEFAULT '', date TEXT, title TEXT, summary TEXT DEFAULT '',
            attribution_method TEXT DEFAULT ''
        )"""
    )
    rows = [
        ("2026-09-01", "广电计量：中报发布", "", "002967", "vocab_colon"),
        ("2026-09-01", "兴业银行：净利411亿", "", "601166", "vocab_colon"),
        ("2026-09-01", "广电计量获机构调研", "", "002967", "vocab_title"),
        ("2026-09-01", "证监会发布新规", "", "", ""),
    ]
    conn.executemany(
        "INSERT INTO stock_news (date, title, summary, code, "
        "attribution_method) VALUES (?,?,?,?,?)",
        list(rows) + list(extra_rows),
    )
    conn.commit()
    return db


# ---------- 1. 默认关零行为 (哨兵宪法) ----------

def test_audit_sentinel_off_by_default_zero_behavior():
    """铁律1: enforce 默认 false — 关态零行为: 不审计, 返回 None,
    不抛不写 (宁可不采, 不默认开哨兵)。"""
    db = _audit_db()
    try:
        res = audit_attribution(db, "2026-09-01")
        assert res is None  # 默认关 = 零行为
    finally:
        db.close()


# ---------- 2. 开态口径三件 ----------

def test_audit_metrics_three_pieces_and_drift_delta():
    """铁律3: 开态 (enforce=true) 口径三件齐 —
    attribution_dist / vocab_drift (size + 日环比 delta, 基线锚定口径) /
    violations (G1/G2/G3) — 全只读, 返回 dict。"""
    db = _audit_db()
    try:
        res = audit_attribution(db, "2026-09-01", enforce=True)
        assert res is not None
        # 件1: 归属分布 (当日行按 method 分组, 空码计 none)
        assert res["attribution_dist"] == {
            "vocab_colon": 2, "vocab_title": 1, "none": 1,
        }
        # 件2: 词表漂移 — size=当日折叠词表实数; 无基线 → delta=None (首采锚定)
        assert res["vocab_drift"]["size"] == 2  # 广电计量+兴业银行
        assert res["vocab_drift"]["delta"] is None
        # 有基线 → 日环比 delta = size - baseline
        res2 = audit_attribution(db, "2026-09-01", enforce=True, vocab_baseline=1)
        assert res2["vocab_drift"] == {"size": 2, "baseline": 1, "delta": 1}
        # 件3: 违例计数三闸全零 (4 行全健康)
        assert res["violations"] == {"G1": 0, "G2": 0, "G3": 0}
    finally:
        db.close()


# ---------- 3. 复验A规则: G1/G2/G3 违例可检 ----------

def test_audit_detects_g1_g2_g3_violations():
    """B 复验 A 的规则: 落库行按卡A三闸复算 —
    G1: code 反查名不在 title (港股午评错归型)
    G2: title 含负向别名却有归属 (比亚迪电子错归型)
    G3: title 双命中却有归属 (取第一个命中型) — 各计数 1。"""
    db = _audit_db(extra_rows=[
        ("2026-09-01", "港股午评：科技股大涨", "", "601166", "vocab_title"),
        ("2026-09-01", "比亚迪电子：上半年溢利降75.4%", "", "002594", "vocab_colon"),
        ("2026-09-01", "广电计量与兴业银行签署合作协议", "", "002967", "vocab_title"),
    ])
    try:
        # 词表需含比亚迪 (G2 复验样本)
        conn = db.get_connection()
        conn.execute("INSERT INTO stock_info (code, name) VALUES ('002594', '比亚迪')")
        conn.commit()
        res = audit_attribution(db, "2026-09-01", enforce=True)
        assert res["violations"] == {"G1": 1, "G2": 1, "G3": 1}
    finally:
        db.close()


# ---------- 6. 真配置链可读 (修死import) ----------

def test_audit_config_reads_real_config_chain(monkeypatch, tmp_path):
    """回执补充件 RED→GREEN: _audit_config 原实现 `from common.config
    import Config` 是死 import (common.config 模块不存在, 生产/worktree
    双证 ModuleNotFoundError) — 永远走 except 返回 False, 哨兵开不动。
    正解 API = config_layer (cfg_get 点键读, llm 域同款)。本测走真
    loader 链 (defaults→settings→profiles→local): 写 news 段进
    defaults/data.yaml → _audit_config 读出 enforce True。"""
    cfgdir = tmp_path / "config"
    dfl = cfgdir / "defaults"
    dfl.mkdir(parents=True)
    (dfl / "data.yaml").write_text("news:\n  attribution_audit:\n    enforce: true\n", encoding="utf-8")
    (cfgdir / "settings.yaml").write_text("", encoding="utf-8")

    import data_layer.pipelines.news as news_mod
    from config_layer.loader import ConfigLoader
    from config_layer.registry import ConfigRegistry

    reg = ConfigRegistry()
    ConfigLoader().load_profile(reg, "prod", str(cfgdir))
    monkeypatch.setattr(
        news_mod, "cfg_get",
        lambda key, default=None: reg.get(key, default),
    )
    assert news_mod._audit_config() == {"enforce": True}


def test_audit_config_off_when_no_section(monkeypatch, tmp_path):
    """反例: config 无 news 段 → enforce False (哨兵宪法默认关)。
    走真 loader 路径非 monkeypatch 直接喂值。"""
    import data_layer.pipelines.news as news_mod
    from config_layer.loader import ConfigLoader
    from config_layer.registry import ConfigRegistry

    cfgdir = tmp_path / "config"
    dfl = cfgdir / "defaults"
    dfl.mkdir(parents=True)
    (dfl / "data.yaml").write_text("other: {}\n", encoding="utf-8")
    (cfgdir / "settings.yaml").write_text("", encoding="utf-8")

    reg = ConfigRegistry()
    ConfigLoader().load_profile(reg, "prod", str(cfgdir))
    monkeypatch.setattr(
        news_mod, "cfg_get",
        lambda key, default=None: reg.get(key, default),
    )
    assert news_mod._audit_config() == {"enforce": False}


# ---------- 4. 段内 try 不杀主管道 ----------

def test_audit_exception_swallowed_not_in_pipeline_errors(monkeypatch):
    """铁律2: 哨兵段内异常只 warning, 不进主管道 errors 不杀 run() —
    06fc3b3 先例。哨兵依赖表被抽走 → 哨兵段炸 → run() 照常返回。"""
    db = _audit_db()
    try:
        conn = db.get_connection()
        conn.execute("DROP TABLE stock_news")  # 哨兵依赖表没了 → 段内炸
        conn.commit()
        monkeypatch.setattr(news_mod, "_audit_config", lambda: {"enforce": True})
        monkeypatch.setattr(
            news_mod, "NewsCollector",
            type("S", (), {"__init__": lambda self, **k: None,
                          "collect": staticmethod(lambda date, **k: 0)}),
        )
        pipe = NewsPipeline(db=db)
        result = pipe.run(PipelineInput(date="2026-09-01"))
        assert result.status == "success"  # 主管道不受哨兵牵连
        assert [e for e in result.errors if "audit" in e] == []
    finally:
        db.close()


# ---------- 5. 口径 metadata 首落 ----------

def test_run_metadata_carries_audit_metrics(monkeypatch):
    """验收微缩: 开态下 run() 的 PipelineResult.metadata["attribution_audit"]
    首落口径三件 (B 复验 A 的接线点, decay_alerts 同型)。"""
    db = _audit_db()
    try:
        monkeypatch.setattr(news_mod, "_audit_config", lambda: {"enforce": True})
        monkeypatch.setattr(
            news_mod, "NewsCollector",
            type("S", (), {"__init__": lambda self, **k: None,
                          "collect": staticmethod(lambda date, **k: 0)}),
        )
        pipe = NewsPipeline(db=db)
        result = pipe.run(PipelineInput(date="2026-09-01"))
        meta = result.metadata["attribution_audit"]
        assert meta["attribution_dist"]["vocab_colon"] == 2
        assert meta["vocab_drift"]["size"] == 2
        assert meta["violations"] == {"G1": 0, "G2": 0, "G3": 0}
    finally:
        db.close()

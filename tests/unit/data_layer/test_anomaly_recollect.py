# -*- coding: utf-8 -*-
"""TDD: shortterm_recollect 16:30 补采轮 (fix/anomaly-1630-recollect)

派单 sm-7c5089a73bd44eaf 第2件 (刀2)。病灶: anomaly-analysis-list 端点
无日期参数 (fuyao_client.py:267) = 纯当日快照, 上游发布晚于 15:44
采集窗 — 09-29 16:48 采得 200 行 vs 10-09 15:44 空; 错过即永久丢
(DL-07 不可回填设计)。10-19 anomaly 402 行坐实「晚发布已自愈于当日
更晚时刻」— 但 15:44 首轮窗仍可能漏, 补采轮治本。

设计 (统筹核准两条):
1. 独立顶层小管道 shortterm_recollect (照 concept_plates_daily 先例:
   独立排程错峰, 不进 DAILY_SUB_PIPELINES, GO-7 硬闸语义保持) —
   16:30 窗 (DDP 实测 15:30→15:44 稳定 ~14min, 46min 隔离; 16:20/16:22
   DL-33 双门禁之后不扰门禁读数)。
2. 幂等且不覆盖当日真值: INSERT OR REPLACE 语义下, 补采覆盖必须在
   报文/元数据留痕 (recollect_overwrote), 不静默覆盖。同日同键以补采轮
   为准 (更晚快照), 但首轮真值与补采值不同时显形。
3. 只跑快照类六项 (limit_ladder/limit_break/hot_stock_history/
   skyrocket/anomaly/sector_fund_flow), 绝不碰 kline/valuation 主腿 —
   集合断言防后人加腿。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from common.database import Database
from data_layer.pipelines.base import PipelineInput

SNAPSHOT_SIX = (
    "limit_ladder", "limit_break", "hot_stock_history",
    "skyrocket", "anomaly", "sector_fund_flow",
)


def _db_with_tables():
    db = Database(":memory:")
    db.init_db()
    conn = db.get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS limit_up_ladder (
            code TEXT, snapshot_date TEXT, trade_date TEXT, board_level TEXT,
            name TEXT, PRIMARY KEY(code, snapshot_date, board_level));
        CREATE TABLE IF NOT EXISTS limit_up_ladder_meta (
            snapshot_date TEXT PRIMARY KEY, window_length INTEGER,
            board_caps_json TEXT, date_list_json TEXT);
        CREATE TABLE IF NOT EXISTS limit_break_pool (
            code TEXT, date TEXT, name TEXT, last_price REAL,
            PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS hot_stock_history (
            code TEXT, date TEXT, name TEXT, rank INTEGER,
            PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS skyrocket_list (
            code TEXT, date TEXT, name TEXT, rank INTEGER, heat REAL,
            PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS stock_anomaly_reasons (
            code TEXT, date TEXT, name TEXT, tag_name TEXT,
            analysis_content TEXT, keywords TEXT, PRIMARY KEY(code, date));
        CREATE TABLE IF NOT EXISTS sector_fund_flow (
            name TEXT, date TEXT, sector_type TEXT, sector_value REAL,
            PRIMARY KEY(name, date));
        CREATE TABLE IF NOT EXISTS daily_klines (
            code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
    """)
    conn.commit()
    return db, conn


class _Src:
    """夹具源: rows=返给定行 (空 list=该类型无数据)。"""

    CAPS = set(SNAPSHOT_SIX)  # 六项都声明 — get_sources_for 按 capabilities 过滤

    def __init__(self, name, rows):
        self.name = name
        self.capabilities = set(self.CAPS)
        self._rows = rows

    def is_available(self):
        return True

    def get_data(self, data_type, params):
        return self._rows


def _reg(*srcs):
    from data_layer.sources.registry import SourceRegistry
    reg = SourceRegistry()
    for i, s in enumerate(srcs, start=1):
        reg.register(s, priority=i, type_priorities={})
    return reg


class TestRecollectSnapshotOnly:
    def test_collectors_are_snapshot_six(self):
        """钉2: 补采轮只跑快照类六项 — 集合断言防后人加 kline/valuation 主腿。"""
        from data_layer.pipelines.shortterm_collect import (
            ShortTermCollectPipeline)
        collectors = ShortTermCollectPipeline._snapshot_collectors()
        names = tuple(name for name, _ in collectors)
        assert names == SNAPSHOT_SIX, (
            f"补采轮必须恰为快照六项 (顺序固定); 实得 {names}")
        for forbidden in ("kline", "valuation", "moneyflow", "fuyao_financial"):
            assert forbidden not in names, (
                f"{forbidden} 是主腿, 不得进补采轮")

    def test_recollect_pipeline_name_and_registration(self):
        """补采轮是独立管道名 shortterm_recollect (与首轮 shortterm_collect 区分,
        pipeline_runs 可分别对账 — GO-7 硬闸语义)。"""
        from data_layer.pipelines.shortterm_recollect import (
            ShortTermRecollectPipeline)
        assert ShortTermRecollectPipeline.name == "shortterm_recollect"


class TestRecollectBehavior:
    def test_first_round_empty_recollect_fills(self):
        """核心: 首轮 0 行的项, 补采轮补上 (anomaly 晚发布治本)。"""
        from data_layer.pipelines.shortterm_recollect import (
            ShortTermRecollectPipeline)
        db, conn = _db_with_tables()
        # 首轮: anomaly 0 行; 其他项有行
        first = _reg(
            _Src("fuyao", []),  # limit_ladder 假空不影响本断言
        )
        pipe = ShortTermRecollectPipeline(db=db, source_registry=first)
        # 补采轮源: anomaly 返 2 行
        rec_src = _reg(_Src("fuyao", [
            {"thscode": "000001.SZ", "stock_name": "平安银行",
             "tag_name": "涨幅", "analysis_content": "x", "keyword_list": ["a"]},
            {"thscode": "000002.SZ", "stock_name": "万科A",
             "tag_name": "量比", "analysis_content": "y", "keyword_list": ["b"]},
        ]))
        pipe2 = ShortTermRecollectPipeline(db=db, source_registry=rec_src)
        out = pipe2.run(PipelineInput(date="2026-10-20", db=db))
        n = conn.execute(
            "SELECT COUNT(*) FROM stock_anomaly_reasons "
            "WHERE date='2026-10-20'").fetchone()[0]
        assert n == 2, f"补采轮必须把 anomaly 补上当日期; 实得 {n}"

    def test_overwrite_leaves_trace(self):
        """钉1: 补采覆盖必须留痕 — 首轮有值的项被补采改写时,
        metadata 显形 recollect_overwrote, 不静默覆盖。"""
        from data_layer.pipelines.shortterm_recollect import (
            ShortTermRecollectPipeline)
        db, conn = _db_with_tables()
        d = "2026-10-20"
        # 首轮已写 1 行 (值 rank=1)
        conn.execute(
            "INSERT INTO hot_stock_history (code, date, name, rank) "
            "VALUES ('600519', ?, '贵州茅台', 1)", (d,))
        conn.commit()
        # 补采轮: 同键不同值 (rank=3, 源侧盘中变动)
        rec = _reg(_Src("fuyao", [
            {"thscode": "600519.SH", "ticker": "600519",
             "name": "贵州茅台", "rank": 3},
        ]))
        pipe = ShortTermRecollectPipeline(db=db, source_registry=rec)
        out = pipe.run(PipelineInput(date=d, db=db))
        row = conn.execute(
            "SELECT rank FROM hot_stock_history WHERE code='600519' AND date=?",
            (d,)).fetchone()
        assert row[0] == 3, "同日同键以补采轮为准 (更晚快照)"
        overwrote = (out.metadata or {}).get("recollect_overwrote") or []
        assert any("hot_stock_history" in str(w) for w in overwrote), (
            f"覆盖必须留痕 recollect_overwrote; 实得 metadata={out.metadata}")

    def test_same_value_no_false_overwrite_trace(self):
        """反向钉: 补采值与首轮相同 → 不得报 overwrote (防刷痕迹)。"""
        from data_layer.pipelines.shortterm_recollect import (
            ShortTermRecollectPipeline)
        db, conn = _db_with_tables()
        d = "2026-10-20"
        conn.execute(
            "INSERT INTO hot_stock_history (code, date, name, rank) "
            "VALUES ('600519', ?, '贵州茅台', 1)", (d,))
        conn.commit()
        rec = _reg(_Src("fuyao", [
            {"thscode": "600519.SH", "ticker": "600519",
             "name": "贵州茅台", "rank": 1},
        ]))
        pipe = ShortTermRecollectPipeline(db=db, source_registry=rec)
        out = pipe.run(PipelineInput(date=d, db=db))
        overwrote = (out.metadata or {}).get("recollect_overwrote") or []
        assert not overwrote, f"同值不得报覆盖; 实得 {overwrote}"

    def test_recollect_does_not_touch_kline(self):
        """钉2 对偶: 补采轮绝不写 daily_klines (主腿不属于补采)。"""
        from data_layer.pipelines.shortterm_recollect import (
            ShortTermRecollectPipeline)
        db, conn = _db_with_tables()
        d = "2026-10-20"
        conn.execute(
            "INSERT INTO daily_klines (code, date, close) "
            "VALUES ('600519', ?, 1500.0)", (d,))
        conn.commit()
        rec = _reg(_Src("fuyao", [
            {"thscode": "600519.SH", "ticker": "600519",
             "name": "贵州茅台", "rank": 1},
        ]))
        pipe = ShortTermRecollectPipeline(db=db, source_registry=rec)
        pipe.run(PipelineInput(date=d, db=db))
        row = conn.execute(
            "SELECT close FROM daily_klines WHERE code='600519' AND date=?",
            (d,)).fetchone()
        assert row[0] == 1500.0, "补采轮不得碰 daily_klines"

    def test_empty_source_honest_no_fake(self):
        """补采轮源也空 → 不造假值, 记 errors (诚实空窗)。"""
        from data_layer.pipelines.shortterm_recollect import (
            ShortTermRecollectPipeline)
        db, conn = _db_with_tables()
        pipe = ShortTermRecollectPipeline(
            db=db, source_registry=_reg(_Src("fuyao", [])))
        out = pipe.run(PipelineInput(date="2026-10-20", db=db))
        n = conn.execute(
            "SELECT COUNT(*) FROM stock_anomaly_reasons").fetchone()[0]
        assert n == 0, "源空不得造假行"
        assert out.errors, "空窗必须留痕 errors"


class TestScheduleRegistration:
    def test_ops_yaml_has_1630_entry(self):
        """config/defaults/ops.yaml 必须有 16:30 排程项 (独立顶层, 不进 DDP)。"""
        import re
        cfg_path = (Path(__file__).resolve().parents[3]
                    / "config" / "defaults" / "ops.yaml")
        text = cfg_path.read_text(encoding="utf-8")
        m = re.search(r"shortterm_recollect:\s*\n\s*schedule:\s*\"(\d{2}:\d{2})\"",
                      text)
        assert m, "ops.yaml 缺 shortterm_recollect 排程项 (建了没通电第九变体)"
        assert m.group(1) == "16:30", f"补采轮应排 16:30; 实得 {m.group(1)}"

    def test_handler_registered_in_orchestrator(self):
        """orchestrator handler 表必须注册 (GO-7: 未注册=跳过+告警)。"""
        from ops_layer.orchestrator import PipelineOrchestrator
        src = Path(
            PipelineOrchestrator.__module__.replace(".", "/") + ".py")
        # 从 src 树找
        import subprocess
        out = subprocess.run(
            ["grep", "-n", "shortterm_recollect",
             str(Path(__file__).resolve().parents[3] / "src"
                 / "ops_layer" / "orchestrator.py")],
            capture_output=True, text=True)
        assert "shortterm_recollect" in out.stdout, (
            "orchestrator 未注册 shortterm_recollect handler")


class TestRecollectRunsOnlySnapshotSix:
    """钉2 补强 (反向钉2 抓出守卫宽度不足): 守护 recollect **实际跑的**
    名单, 不只守护单一事实源 — 往 recollect 侧拼接主腿必须红。"""

    def test_recollect_collectors_are_snapshot_six(self):
        """recollect 侧 collectors 必须恰为快照六项 (拼接过主腿即红)。"""
        from data_layer.pipelines.shortterm_recollect import (
            ShortTermRecollectPipeline)
        from data_layer.pipelines.shortterm_collect import (
            ShortTermCollectPipeline)
        collectors = ShortTermCollectPipeline._snapshot_collectors()
        names = [n for n, _ in collectors]
        assert names == list(SNAPSHOT_SIX), f"六项名单漂移: {names}"
        for forbidden in ("kline", "valuation", "moneyflow", "fuyao_financial"):
            assert forbidden not in names, (
                f"{forbidden} 是主腿, 不得进补采轮名单")


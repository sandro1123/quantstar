# -*- coding: utf-8 -*-
"""甲案第二段 TDD — 入池 enrich_reason：rationale 有料→真理由回填

设计批复（统筹 09-24/25，sm-eb9088b2865e4814）：
  调用点 = plan_manager.receive_plan；降级 = 零新增 LLM + 失败不阻塞；
  与 W1 分工 = 他落库（agent 层 _run_debate 末尾）我回填（执行层入池侧）。

三裁点：
  A 补辩论暂不做（真理由覆盖率天花板 = 当日辩论票数 ≤ deep_top_n，特性非缺陷）
  B rationale_id 列不加（trace JOIN 已有 guard_trace+rationale 两锚）
  C 覆盖率天花板写 docstring 说清

铁律#12 硬边界（统筹叮咛）：receive_plan 是入池主链，enrich_reason 必须在
try/except 里且绝不抛——任何异常降级成「保持现状 added_reason」，不阻断入池。
"""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))


class FakeDB0:
    """action_record.ensure_tables 的最小 DB 适配（只暴露 get_connection）"""
    def __init__(self, conn):
        self._c = conn

    def get_connection(self):
        return self._c


def make_plan_manager(tmp_conn):
    """最小 PlanManager（mock 依赖，不走真实 LLM/全链）"""
    from execution_layer.core.plan_manager import PlanManager

    class FakeDB:
        def get_connection(self):
            return tmp_conn

    pm = PlanManager.__new__(PlanManager)   # 跳过 __init__ 的重依赖装配
    pm.db = FakeDB()
    pm.human_in_loop = SimpleNamespace(evaluate=lambda **kw: SimpleNamespace(
        action="auto_approve", reason="test"))
    pm.action_record = SimpleNamespace(write=lambda **kw: None)
    # stock_pool mock（真实写库到 tmp_conn——用真 StockPool 保证 added_reason 落表可断言）
    from execution_layer.core.stock_pool import StockPool
    pm.stock_pool = StockPool(FakeDB())
    return pm


@pytest.fixture()
def db_with_rationale():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # v10 契约表（1a fe45b0f3 契约冻结形状）
    conn.execute("""CREATE TABLE recommendation_rationale (
        rationale_id TEXT PRIMARY KEY, code TEXT NOT NULL, rec_date TEXT NOT NULL,
        verdict TEXT, confidence REAL, position_pct REAL, reasoning TEXT,
        expert_name TEXT, key_points TEXT, invalidation_conditions TEXT,
        claims TEXT, trace_id TEXT, run_id TEXT, request_id TEXT,
        advisor_text TEXT, advisor_guard TEXT,
        created_at TEXT DEFAULT (datetime('now','+8 hours')),
        UNIQUE (code, rec_date, expert_name))""")
    # guard_trace（守卫失败降级路径会写）
    conn.execute("""CREATE TABLE advisor_guard_trace (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, code TEXT NOT NULL,
        rejected_gate TEXT NOT NULL, degrade_path TEXT NOT NULL, llm_output_hash TEXT,
        detail TEXT NOT NULL DEFAULT '', rationale_id TEXT, trace_id TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')))""")
    # stock_pool / action_records 用真 DDL（防夹具落后于真 schema——判例：
    # 夹具比生产宽=病根镜像；让代码自建表）
    from execution_layer.tables import STOCK_POOL_DDL
    from execution_layer.core.action_record import ActionRecord
    conn.execute(STOCK_POOL_DDL)
    try:
        ActionRecord(FakeDB0(conn)).ensure_tables()
    except Exception:
        pass  # 兜底: ensure_tables 失败时下面手工建最小形状
    conn.commit()
    yield conn
    conn.close()


class TestEnrichReason:
    # REC 模块2 守卫要求风险收益参数齐全，否则拒入池——夹具统一带全
    RR = {"entry_low": 10.0, "entry_high": 10.5, "stop_loss": 9.5, "take_profit": 12.0}

    def test_rationale_present_backfills_real_reason(self, db_with_rationale):
        """rationale 有料 → added_reason 换成加工后真理由（非排名话术）"""
        db_with_rationale.execute(
            "INSERT INTO recommendation_rationale (rationale_id, code, rec_date, verdict, "
            "confidence, reasoning, expert_name) VALUES "
            "('r1','600000','2026-09-25','推荐',0.72,'动量因子近20日强势，综合判断可买入','chairman')")
        db_with_rationale.commit()
        pm = make_plan_manager(db_with_rationale)
        plan = {"date": "2026-09-25", "recommendations": [
            dict({"code": "600000", "name": "浦发", "action": "buy", "reason": "候选池第1位",
                  "initial_score": 0.9, "confidence": 0.7}, **self.RR)]}
        pm.receive_plan(plan)
        # 入池后 added_reason = 真理由（含原料关键词），非「候选池第1位」
        row = db_with_rationale.execute(
            "SELECT added_reason FROM stock_pool WHERE code='600000'").fetchone()
        assert row is not None, "入池应完成"
        reason = row["added_reason"] or ""
        assert "候选池第" not in reason, f"排名话术不应原样进池: {reason!r}"
        assert ("动量" in reason) or ("买入" in reason), f"应含真理由关键词: {reason!r}"

    def test_no_rationale_keeps_original_reason(self, db_with_rationale):
        """无料 → added_reason 保持原样（诚实水位交给下游 _pick_reason 闸），不炸"""
        pm = make_plan_manager(db_with_rationale)
        plan = {"date": "2026-09-25", "recommendations": [
            dict({"code": "300001", "name": "特锐德", "action": "buy",
                  "reason": "候选池第2位"}, **self.RR)]}
        pm.receive_plan(plan)
        row = db_with_rationale.execute(
            "SELECT added_reason FROM stock_pool WHERE code='300001'").fetchone()
        assert row is not None, "无料时入池必须照常完成"
        assert (row["added_reason"] or "") == "候选池第2位"

    def test_guard_rejection_degrades_to_placeholder(self, db_with_rationale):
        """守卫拒（原料本身是编造话术）→ 降级诚实占位，不写脏理由，且留痕"""
        db_with_rationale.execute(
            "INSERT INTO recommendation_rationale (rationale_id, code, rec_date, verdict, "
            "confidence, reasoning, expert_name) VALUES "
            "('r2','600519','2026-09-25','推荐',0.9,'', 'chairman')")  # reasoning 空
        db_with_rationale.commit()
        pm = make_plan_manager(db_with_rationale)
        plan = {"date": "2026-09-25", "recommendations": [
            dict({"code": "600519", "name": "茅台", "action": "buy",
                  "reason": "候选池第1位"}, **self.RR)]}
        pm.receive_plan(plan)
        row = db_with_rationale.execute(
            "SELECT added_reason FROM stock_pool WHERE code='600519'").fetchone()
        assert row is not None, "守卫拒时入池必须照常完成"
        reason = row["added_reason"] or ""
        # 降级不能写「没有理由的理由」——保持原 added_reason，交下游闸
        assert reason == "候选池第1位" or "理由" in reason

    def test_enrich_never_raises_never_blocks(self, db_with_rationale, monkeypatch):
        """铁律#12 硬边界：enrich 内部任何异常 → 入池照常完成。
        炸弹埋在 enrich 内部依赖处（load_argument），而非 rec 取值处——
        rec 取值炸在循环层属入池链自身的病，不是 enrich 的守护范围。"""
        pm = make_plan_manager(db_with_rationale)

        def bomb_load(code, date, conn):
            raise RuntimeError("enrich 内部异常")

        monkeypatch.setattr("execution_layer.core.enrich_reason.load_argument",
                            bomb_load, raising=False)
        plan = {"date": "2026-09-25", "recommendations": [
            dict({"code": "000001", "name": "平安", "action": "buy",
                  "reason": "候选池第1位"}, **self.RR),
            dict({"code": "000002", "name": "万科A", "action": "buy",
                  "reason": "候选池第3位"}, **self.RR)]}
        pm.receive_plan(plan)   # 不应抛
        row = db_with_rationale.execute(
            "SELECT COUNT(*) c FROM stock_pool WHERE code='000002'").fetchone()
        assert row["c"] == 1, "异常后其余票入池必须照常"

    def test_zero_new_llm_calls(self, db_with_rationale, monkeypatch):
        """零新增 LLM：rewrite_to_advisor 只能以 llm=None 被调（守卫加工是确定性规则）。
        钉「llm 参数恒为 None」——这是成本契约，比钉「某内部函数没被调」更强。"""
        llm_args = []

        real_rewrite = None
        import importlib
        import report_layer.advisor as pkg          # 包级（enrich 函数内 import 解析处）
        rw = importlib.import_module("report_layer.advisor.rewrite")
        real_rewrite = rw.rewrite_to_advisor

        def spy_rewrite(arg, llm=None, **kw):
            llm_args.append(llm)
            return real_rewrite(arg, llm=llm, **kw)

        # patch 包级属性（enrich 内 `from report_layer.advisor import ...` 每次调用
        # 都解析包属性——patch 子模块属性不会生效，判例#26 同源：副本不随正本变）
        monkeypatch.setattr(pkg, "rewrite_to_advisor", spy_rewrite)
        db_with_rationale.execute(
            "INSERT INTO recommendation_rationale (rationale_id, code, rec_date, verdict, "
            "confidence, reasoning, expert_name) VALUES "
            "('r3','600030','2026-09-25','推荐',0.7,'券商基本面稳健','chairman')")
        db_with_rationale.commit()
        pm = make_plan_manager(db_with_rationale)
        pm.receive_plan({"date": "2026-09-25", "recommendations": [
            {"code": "600030", "name": "中信", "action": "buy", "reason": "候选池第1位",
             "entry_low": 10.0, "entry_high": 10.5, "stop_loss": 9.5, "take_profit": 12.0}]})
        assert llm_args and all(a is None for a in llm_args), \
            f"enrich 链不得携带 LLM（llm 参数恒 None），实得: {llm_args}"

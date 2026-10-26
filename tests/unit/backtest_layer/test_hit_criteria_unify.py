"""卡IV前半 RED — AG/BT hit口径统一对拍测试

背景 (统筹 sm-cdc32527a6304767, 来源 D 的 v2 P1/P2 对账):
  AG 侧 evaluator.py:41 观望/警惕均走 abs<0.02 宽判=算命中;
  BT 侧 agent_eval.py:34 警惕落"未命中"窄判。同一 verdict 两套口径
  → agent_eval 命中率统计被污染, 反馈闭环不可信。

裁定 (以信号语义/PIT 为准, F 2026-08-30):
  警惕 = 中性带成员 (output_spec AG-03: 风险提示不构成方向阻断,
  无方向主张不能用方向兑现判错), 统一采用 AG 宽判口径:
  观望/警惕 + |actual|<2% → hit=1。BT is_hit 是抄 AG 语义漏枚举值
  (其 docstring 自称"语义对齐"), 修法=BT 侧补齐+归一化 legacy 词。

本文件三段:
  1. 对拍测试 — 同一 (verdict, actual) 组合喂 AG Evaluator 落库 hit
     与 BT is_hit, 断言恒等 (单口径钉死);
  2. 警惕宽判 — BT is_hit("警惕", 小波动)=True / 大波动=False;
  3. legacy 词归一化 — BT is_hit("hold"/"watch"/"neutral", 小波动)
     与 AG normalize 后行为一致 (隐藏分叉第二层收口)。
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from common.database import Database


PRED_DDL = """
CREATE TABLE IF NOT EXISTS agent_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT, date TEXT, verdict TEXT, confidence REAL,
    actual_return REAL, hit INTEGER, created_at TEXT
)
"""


def _seed_pred(db, code, verdict, confidence=0.5, date="2026-07-17"):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO agent_predictions (code, date, verdict, confidence) "
        "VALUES (?, ?, ?, ?)", (code, date, verdict, confidence))
    conn.commit()


def _ag_hit(db, code, actual, date="2026-07-17"):
    """走 AG Evaluator 全链 (含 normalize_verdict) 落库, 取回写 hit。

    用 repo 同一 get_connection (Database 缓存单连接) 查; 另以
    Evaluator 返回 dict (hits/misses) 双通道断言原料 — 落库行为准,
    返回值交叉防"查得到但写错"。
    """
    from agent_layer.quality.evaluator import Evaluator
    res = Evaluator(database=db).evaluate(date, {code: actual})
    assert res["evaluated"] == 1, f"AG 未结算该行: {res}"
    conn = db.get_connection()
    row = conn.execute(
        "SELECT hit FROM agent_predictions WHERE code = ? ORDER BY id DESC",
        (code,)).fetchone()
    assert row is not None, "AG 落库行丢失 (测试基建问题, 非口径问题)"
    return row["hit"]


# ---------- RED 1: 对拍 (AG 落库 hit == BT is_hit) ----------

_CROSS_CASES = [
    # (verdict, actual) — 覆盖方向判定+中性带宽+警惕+边界
    ("推荐", 0.05), ("推荐", -0.01), ("推荐", 0.0),
    ("不推荐", -0.05), ("不推荐", 0.01), ("不推荐", 0.0),
    ("观望", 0.01), ("观望", -0.019), ("观望", 0.02), ("观望", -0.05),
    ("警惕", 0.01), ("警惕", -0.019), ("警惕", 0.05), ("警惕", -0.05),
    ("neutral", 0.005), ("hold", 0.005), ("watch", 0.03),
    ("approve", 0.01), ("reject", -0.01),
]


def test_cross_judgment_ag_bt_identical():
    """同一 (verdict, actual) 喂两处判定, 结果必须恒等 — 口径分叉即红。"""
    from backtest_layer.evaluation.agent_eval import is_hit
    for verdict, actual in _CROSS_CASES:
        db = Database(":memory:")
        conn = db.get_connection()
        conn.executescript(PRED_DDL)
        _seed_pred(db, "000001", verdict)
        ag = _ag_hit(db, "000001", actual)
        bt = 1 if is_hit(verdict, actual) else 0
        assert ag == bt, (
            f"口径分叉: verdict={verdict!r} actual={actual} "
            f"AG落库hit={ag} BT is_hit={bt} — 两处必须单口径 (卡IV RED)")
        db.close()


# ---------- RED 2: 警惕宽判 (BT 侧钉死新口径) ----------

def test_bt_warning_verdict_small_move_is_hit():
    """警惕+|actual|<2% = 命中 (与 AG/观望同构 — 风险提示无方向主张,
    落入小波动容差带即"判对无明确方向"); 警惕+大波动 = 未命中。"""
    from backtest_layer.evaluation.agent_eval import is_hit
    assert is_hit("警惕", 0.019) is True
    assert is_hit("警惕", -0.019) is True
    # 大波动: 无方向主张但事后有大方向 → 判对无明确方向不成立 = miss
    assert is_hit("警惕", 0.05) is False
    assert is_hit("警惕", -0.05) is False


# ---------- RED 3: legacy 词归一化 (BT 补齐隐藏分叉) ----------

def test_bt_legacy_verdicts_normalized_like_ag():
    """BT is_hit 须先 normalize_verdict — legacy 词 (hold/watch/neutral/
    approve/reject) 与 AG 归一化后行为一致; 未知词诚实落观望 (宽判带)。"""
    from backtest_layer.evaluation.agent_eval import is_hit
    # legacy 中性词 → 观望: 小波动命中, 大波动 miss
    assert is_hit("hold", 0.005) is True
    assert is_hit("watch", 0.005) is True
    assert is_hit("watch", 0.03) is False
    # legacy 方向词 → 推荐/不推荐
    assert is_hit("approve", 0.01) is True     # approve→推荐 (方向带)
    assert is_hit("reject", -0.01) is True
    # 未知词诚实落观望 (AG 同款): _LEGACY_MAP 只有中文"买入", 英文
    # "buy" 非法词 → 观望 — 小波动命中, 大波动 miss
    assert is_hit("buy", 0.005) is True
    assert is_hit("buy", 0.05) is False
    # 中文 legacy 方向词 → 推荐: 大波动命中 (方向兑现)
    assert is_hit("买入", 0.05) is True

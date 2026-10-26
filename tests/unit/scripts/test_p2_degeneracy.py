"""② p2 页 ICIR 排序键退化告警 + 07-19 批口径页注。

F席 E6 问题① 同批施工 (统筹 0270ea38 裁定)。

病 (F席 09-16 零负载实测, calc_date=2026-09-01, 87 行):
  `icir` 全 NULL → `ORDER BY icir DESC` 与 `ASC` **同序** (均退化为 factor_name ASC)
  → TOP10 与 BOT10 **逐位相同**; BOT5 == TOP10[:5] (BOT 是 TOP 的前缀)
  → 「ICIR Top/Bottom」两表展示同一批因子 = **假分化**。

判据 (统筹裁定): **`COUNT(DISTINCT icir) <= 1`** —— 「排序键无区分度」。
  ⚠ 原卡判据「TOP==BOT 集合相等」**不可用**: 其答案**取决于取数条数**
    (TOP10 vs BOT5 自然不等 → 恒 False → 告警永不触发 = 假绿)。
  实测数字 (供后来者复现, 统筹要求写准):
    TOP10 = BOT10 (逐位相同) = True
    BOT5  == TOP10[:5]       = True
    COUNT(DISTINCT icir)     = 0
"""
import re
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "marimo_pages"))

_P2 = _ROOT / "scripts" / "marimo_pages" / "p2_factors.py"


def _strip_comments(src: str) -> str:
    """剥掉 # 注释与 docstring —— 反面断言须只看**代码**, 否则测的是措辞。"""
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"'''.*?'''", "", src, flags=re.S)
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


def test_degeneracy_sql_all_null_icir():
    """判据核心: icir 全 NULL → COUNT(DISTINCT icir) = 0 → DEGEN (真 SQL 语义)。"""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE factor_ic (calc_date TEXT, factor_name TEXT, "
               "mean_ic REAL, icir REAL)")
    for i in range(10):
        db.execute("INSERT INTO factor_ic VALUES ('2026-09-01', ?, 0.1, NULL)",
                   (f"alpha_{i:03d}",))
    d = db.execute("SELECT COUNT(DISTINCT icir) FROM factor_ic "
                   "WHERE calc_date = '2026-09-01'").fetchone()[0]
    assert d == 0, f"icir 全 NULL 时 COUNT(DISTINCT icir) 应为 0, 实为 {d}"
    assert d <= 1, "判据 COUNT(DISTINCT icir) <= 1 应判为退化"


def test_degeneracy_sql_real_icir_not_flagged():
    """对照: icir 有真分化 → COUNT(DISTINCT icir) > 1 → 不报退化 (防假红)。"""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE factor_ic (calc_date TEXT, factor_name TEXT, "
               "mean_ic REAL, icir REAL)")
    for i in range(10):
        db.execute("INSERT INTO factor_ic VALUES ('2026-09-01', ?, 0.1, ?)",
                   (f"alpha_{i:03d}", i * 0.1))
    d = db.execute("SELECT COUNT(DISTINCT icir) FROM factor_ic "
                   "WHERE calc_date = '2026-09-01'").fetchone()[0]
    assert d == 10 and d > 1, f"真分化时不应判退化, 实为 {d}"


def test_degeneracy_single_nonnull_still_flagged():
    """边界: 仅 1 个因子有非空 icir → 其余全并列 → 仍退化 (<=1 而非 ==0)。"""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE factor_ic (calc_date TEXT, factor_name TEXT, "
               "mean_ic REAL, icir REAL)")
    db.execute("INSERT INTO factor_ic VALUES ('2026-09-01', 'a', 0.1, 0.5)")
    for i in range(9):
        db.execute("INSERT INTO factor_ic VALUES ('2026-09-01', ?, 0.1, NULL)",
                   (f"alpha_{i:03d}",))
    d = db.execute("SELECT COUNT(DISTINCT icir) FROM factor_ic "
                   "WHERE calc_date = '2026-09-01'").fetchone()[0]
    assert d == 1 and d <= 1, "仅 1 个非空 icir 时排序仍退化 (其余并列)"


def test_p2_page_has_degeneracy_alert():
    """p2 页须装退化告警 (第二道闸, 防下次再有列缺失)。"""
    raw = _P2.read_text(encoding="utf-8")
    code = _strip_comments(raw)
    assert "DEGEN" in code, "p2 未装退化告警变量"
    assert "COUNT(DISTINCT icir)" in code, "p2 未用『排序键无区分度』判据"
    assert "排序键失效" in raw, "p2 缺退化告警文案"


def test_p2_page_degeneracy_uses_distinct_not_set_equality():
    """反面断言: 不得回退到「集合相等」判据 (其答案取决于取数条数 = 假绿)。

    剥注释后断言 —— 页内注释会提到旧判据的措辞 (见 test_p2_staleness 同族判例)。
    """
    code = _strip_comments(_P2.read_text(encoding="utf-8"))
    assert "set(TOP)" not in code and "set(BOT)" not in code, \
        "p2 退化判据回退到集合相等 (恒 False 假绿); 须用 COUNT(DISTINCT icir)"


def test_p2_page_has_legacy_caliber_note_with_upgrade_condition():
    """07-19 批口径页注 (采乙: 仅页注) 须存在, 且含『何时该升级』条件。"""
    raw = _P2.read_text(encoding="utf-8")
    assert "口径说明" in raw, "缺 07-19 批口径页注"
    assert "滚动 20 期" in raw, "页注未写明口径"
    assert "caliber" in raw, "页注缺『须升为 caliber 列』的升级条件 (统筹要求)"

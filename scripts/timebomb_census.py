# -*- coding: utf-8 -*-
"""时点炸弹普查 — 只列不修（统筹 sm-639ef5d5 裁）。

判据（两颗实弹归纳出来的）：
    夹具写死日期字面量  ×  被测面取 datetime.now()/bj_date()/date.today()
两者同时成立 = 该测试**只在某一天绿**，自带保质期。

已引爆两颗，都写于 2026-09-14、都在 09-15 变红：
  R2-04  test_incremental_backup::test_handler_contract
         夹具钉 '2026-09-14'，handler 走 run_incremental_backup(db, dir) 默认 now()
  第二颗 test_orchestrator_truthfulness::test_majority_real_scores_passes_guard
         _harness 钉 '2026-09-14'，_run_factor_update 查 date=今天

**本脚本只产出嫌疑清单，不改任何文件。** 嫌疑 ≠ 有病：
夹具写死日期而被测面也接受显式日期参数的，是完全正常的写法。
判定需人看 —— 这里只负责把「需要人看的那几个」从几百个文件里捞出来。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
SRC, TESTS = ROOT / "src", ROOT / "tests"

DATE_LIT = re.compile(r"""['"](20\d\d-\d\d-\d\d)['"]""")
NOW_CALL = re.compile(r"\b(datetime\.now\(\)|date\.today\(\)|bj_date\(\)|bj_now\(\))")

# 被测面「取今天」的模块清单：src 里哪些模块会自己决定"今天"
now_modules: dict[str, list[str]] = {}
for p in sorted(SRC.rglob("*.py")):
    hits = NOW_CALL.findall(p.read_text(encoding="utf-8", errors="ignore"))
    if hits:
        mod = str(p.relative_to(SRC)).replace("\\", "/")[:-3].replace("/", ".")
        now_modules[mod] = sorted(set(hits))


def imported_modules(tree: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
            out |= {f"{n.module}.{a.name}" for a in n.names}
    return out


suspects = []
for p in sorted(TESTS.rglob("test_*.py")):
    text = p.read_text(encoding="utf-8", errors="ignore")
    dates = DATE_LIT.findall(text)
    if not dates:
        continue
    try:
        tree = ast.parse(text)
    except SyntaxError:
        continue
    imps = imported_modules(tree)
    # 被测面里会自己取今天的模块（前缀匹配：导入 a.b 覆盖 a.b.c）
    touched = sorted({m for m in now_modules
                      if m in imps or any(i == m or m.startswith(i + ".") for i in imps)})
    if not touched:
        continue
    # 该测试自己有没有"今天"的对冲（用 now() 造夹具日期 = 已同源）
    hedged = bool(NOW_CALL.search(text))
    suspects.append({
        "file": str(p.relative_to(ROOT)).replace("\\", "/"),
        "dates": sorted(set(dates)),
        "modules": touched[:4],
        "hedged": hedged,
    })

hot = [s for s in suspects if not s["hedged"]]
warm = [s for s in suspects if s["hedged"]]

print("=== 时点炸弹嫌疑清单（只列不修）===")
print(f"扫描: tests/ 下 test_*.py；src 中会自取「今天」的模块 {len(now_modules)} 个\n")
print(f"【甲类 · 需人看】{len(hot)} 个：写死日期 + 被测模块自取今天 + **测试内无 now() 对冲**")
for s in hot:
    print(f"  {s['file']}")
    print(f"      写死日期 {s['dates'][:4]}{' …' if len(s['dates']) > 4 else ''}")
    print(f"      被测面取今天: {', '.join(s['modules'])}")
print(f"\n【乙类 · 已有对冲，优先级低】{len(warm)} 个（测试内出现过 now()/bj_date()）")
for s in warm:
    print(f"  {s['file']}  {s['dates'][:3]}")

print("\n--- 判读须知 ---")
print("嫌疑 != 有病。夹具写死日期、而被测入口**接受显式日期参数**的，是完全正常的写法")
print("（本仓多数测试属此类：如 run_incremental_backup(db, dir, date='2026-09-14')）。")
print("真炸弹的特征是：**被测入口不接受日期、自己调 now()**，夹具却钉死某天。")
print("逐个判读的最快判据：看该测试调被测函数时**有没有传 date 参数**。")

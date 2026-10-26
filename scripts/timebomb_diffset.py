# -*- coding: utf-8 -*-
"""件2 · 时点炸弹差集普查 —— 两轮全量对拍，零假阳。

## 为什么是差集法，而不是静态判别

上轮我做过静态判别（夹具写死日期 × 被测面调 `now()`），137 → 80 个命中，
**逐个看下来全是绿的**。结论：**静态判别是错的工具** —— 它只能圈出「形状可疑」，
而「形状可疑」与「真的会在某天变红」之间隔着一整个判读。

差集法直接问那个真问题：**把时钟拨到 7 天后，哪些今天绿的用例会变红？**
不需要判读，不需要启发式，**零假阳**（真红就是真红）。

## 为什么必须是 faketime，不能用 Python 打桩

本仓 schema 大量使用 `created_at TEXT DEFAULT (datetime('now','+8 hours'))` ——
这是 **SQLite 在 C 层读进程时钟**算出来的。`faketime` 移得动它；freezegun/time_machine
这类进程内打桩**移不动**。而带日期戳的行正是时点炸弹最常住的地方。
→ 用 Python 打桩会得到一份**带命名盲区**的普查，而带盲区的普查比不做更坏，
  因为它读起来像全清。

实证（本机，装 faketime 后当场验的）：
    真实 sqlite : 2026-09-16 00:49:59
    +7d  sqlite : 2026-09-23 00:49:59   ← C 层时钟确实被移动

## 判据面纪律

- **用 JUnit XML 取逐用例结果**，不解析 `.` 点阵 —— 点阵在 xdist/重排下不可靠，
  且**数不清到底哪一个红了**。
- **双向报差集**：`绿→红` 是炸弹；`红→绿` 也必须报 —— 那说明有用例**只在今天红**，
  同样是时点耦合，只是方向相反（上轮我漏掉了这一侧）。
- **空转守卫**：两轮收集数必须都过下限、且彼此接近。若 faketime 轮收集数骤降，
  说明它在 collect 阶段就炸了 —— 此时「零差集」是空转恒真，**不是结论**。
- **不套管道**：退出码只看 pytest 自己的，不让 `tail`/`grep` 吞掉
  （今夜刚踩过：`timeout … pytest | tail -5` 的 0 是 tail 的 0）。
"""
from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MIN_COLLECTED = 4000        # 全量约 5067；低于此说明 collect 阶段就坏了
MAX_COLLECT_DRIFT = 50      # 两轮收集数之差超过此值 = 有一轮没正常起来


def run_round(label: str, xml_path: Path, faketime: str | None) -> tuple[int, str]:
    """跑一轮全量，结果落 JUnit XML。返回 (pytest 退出码, 命令行)。"""
    cmd = ["python", "-m", "pytest", "tests", "-q", "--no-header",
           "-p", "no:cacheprovider", f"--junit-xml={xml_path}"]
    if faketime:
        cmd = ["faketime", faketime] + cmd
    print(f"  [{label}] {' '.join(cmd)}", flush=True)
    # 不套管道: 退出码必须是 pytest 自己的
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    (xml_path.with_suffix(".stdout")).write_text(p.stdout + p.stderr,
                                                 encoding="utf-8", errors="replace")
    return p.returncode, " ".join(cmd)


def outcomes(xml_path: Path) -> dict[str, str]:
    """{用例ID: 'pass'|'fail'|'error'|'skip'}。ID = classname::name，跨轮可对齐。"""
    root = ET.parse(xml_path).getroot()
    out: dict[str, str] = {}
    for tc in root.iter("testcase"):
        tid = f"{tc.get('classname')}::{tc.get('name')}"
        if tc.find("failure") is not None:
            out[tid] = "fail"
        elif tc.find("error") is not None:
            out[tid] = "error"
        elif tc.find("skipped") is not None:
            out[tid] = "skip"
        else:
            out[tid] = "pass"
    return out


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/timebomb")
    out_dir.mkdir(parents=True, exist_ok=True)
    shift = sys.argv[2] if len(sys.argv) > 2 else "+7 days"

    # ---- 首行: 我在验什么（基座指纹，供复核）
    print("=" * 74)
    print("件2 · 时点炸弹差集普查")
    print(f"  判据   : 基线全量 vs faketime '{shift}' 全量, 逐用例对拍")
    print(f"  炸弹   : 基线 pass -> faketime fail  (**零假阳: 真红就是真红**)")
    print(f"  反向   : 基线 fail -> faketime pass  (只在今天红, 同样是时点耦合)")
    print(f"  产物   : {out_dir}/base.xml, {out_dir}/shifted.xml")
    # ---- faketime 有效性实证: **不能拿 --version 当判据**
    # faketime 不支持 --version, 版本串恒为空 —— 拿它判会把"没版本号"读成"不可用"。
    # 真判据是**它到底移没移动时钟**, 且必须验 **SQLite C 层**那一面:
    # 本仓 schema 大量用 `DEFAULT (datetime('now','+8 hours'))`, 那是 C 层读进程时钟算的,
    # 也正是时点炸弹最常住的地方。进程内 Python 打桩移不动它, faketime 能。
    # **若这里不验**: faketime 万一静默失效, 位移轮就只是基线轮的复本,
    # 差集恒为空 —— 而空差集会被读成「没有炸弹」。**空转恒真, 又一次。**
    probe = "import sqlite3;print(sqlite3.connect(':memory:')." \
            "execute(\"SELECT datetime('now')\").fetchone()[0])"
    real = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True).stdout.strip()
    faked = subprocess.run(["faketime", shift, sys.executable, "-c", probe],
                           capture_output=True, text=True).stdout.strip()
    print(f"  faketime 实证: SQLite C 层 真实={real!r} -> 位移={faked!r}")
    if not faked or faked[:10] == real[:10]:
        print("\n**空转判定**: faketime 没能移动 SQLite C 层时钟 —— "
              "此时两轮等价, 「零炸弹」是空转恒真, 不是结论。")
        return 2
    print("=" * 74, flush=True)

    base_xml, shift_xml = out_dir / "base.xml", out_dir / "shifted.xml"

    rc1, _ = run_round("基线", base_xml, None)
    # **轮间 fail-fast**: 第一轮没产出就立刻停, 不烧第二轮。
    # 实伤 (2026-09-16 01:52): base 轮被外部 kill, 而本驱动照旧跑了 shifted 轮 ——
    # 那 ~50 分钟注定白费, 因为末尾对拍必然判空转。
    # 判例: **多阶段作业的每一阶段都要自证产出, 否则后续阶段是在为一个已死的前提干活。**
    if not base_xml.exists():
        print(f"\n**基线轮未产出** {base_xml} —— 该轮被中断或根本没起来。")
        print("**不继续跑位移轮**: 缺一轮则对拍不成立, 跑了也是白烧。请重跑。")
        return 2

    rc2, _ = run_round(f"faketime {shift}", shift_xml, shift)
    print(f"  pytest 退出码: 基线={rc1}  位移={rc2}  (非 0 正常: 存在预存红)", flush=True)

    if not shift_xml.exists():
        print(f"\n**位移轮未产出** {shift_xml} —— 该轮被中断。对拍不成立, 请重跑。")
        return 2

    base, shifted = outcomes(base_xml), outcomes(shift_xml)

    # ---- 守卫的守卫: 收集面塌了的话, 「零差集」是空转恒真
    print(f"\n收集面: 基线 {len(base)} 例 / 位移 {len(shifted)} 例")
    if len(base) < MIN_COLLECTED or len(shifted) < MIN_COLLECTED:
        print(f"**空转判定**: 收集数低于下限 {MIN_COLLECTED} —— collect 阶段即失败。")
        return 2
    if abs(len(base) - len(shifted)) > MAX_COLLECT_DRIFT:
        print(f"**空转判定**: 两轮收集数相差 {abs(len(base)-len(shifted))} "
              f"> {MAX_COLLECT_DRIFT} —— 有一轮没正常起来, 差集不可信。")
        return 2

    common = set(base) & set(shifted)
    bombs = sorted(t for t in common
                   if base[t] == "pass" and shifted[t] in ("fail", "error"))
    reverse = sorted(t for t in common
                     if base[t] in ("fail", "error") and shifted[t] == "pass")
    only_base = sorted(set(base) - set(shifted))
    only_shift = sorted(set(shifted) - set(base))

    print(f"\n{'='*74}")
    print(f"【时点炸弹】基线 pass -> faketime fail : **{len(bombs)} 个**")
    for t in bombs:
        print(f"    {t}")
    print(f"\n【反向】基线 fail -> faketime pass : {len(reverse)} 个 "
          f"(只在今天红, 同样是时点耦合)")
    for t in reverse:
        print(f"    {t}")
    if only_base or only_shift:
        print(f"\n[探针提示, 非发现] 仅一轮出现的用例: 基线独有 {len(only_base)} / "
              f"位移独有 {len(only_shift)} (参数化 ID 含日期时会这样)")
        for t in (only_base[:5] + only_shift[:5]):
            print(f"    {t}")

    base_red = sum(1 for v in base.values() if v in ("fail", "error"))
    shift_red = sum(1 for v in shifted.values() if v in ("fail", "error"))
    print(f"\n对拍 {len(common)} 例 ｜ 基线红 {base_red} / 位移红 {shift_red} "
          f"｜ 炸弹 {len(bombs)} · 反向 {len(reverse)}")
    print("注: 基线红是**预存债**, 不是本次发现; 炸弹 = 两轮之差, 与预存债无关。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())

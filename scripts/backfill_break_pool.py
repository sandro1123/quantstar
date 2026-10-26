# -*- coding: utf-8 -*-
"""backfill_break_pool.py — 刀5: limit_break_pool 缺口回填 (dry-run 默认)

卡组二修法五刀之刀5 (待用户授权链, --apply 由统筹亲执)。
端点能力与**当下供数状态**必须分清 (统筹 sm-50532a3814534ed5 关键区分):
- 能力面: limit-break-pool 端点**带 date 参数** (fuyao_client.py:202-207,
  `_fetch_paged_pool(path, date)` 逐页传 date), anomaly-analysis-list
  **无 date 参数** (纯当日快照, 只能靠 16:30 补采轮刀2 防再犯)。
- 供数面 (2026-10-20 实测): 端点**当下整体不供数** — 15 个候选缺口日
  + 库里已有数据的对照日 (10-12/10-09/09-29) + 当日快照 (无 date 参数)
  全部 code=2003 rows=0 (非限流, failure_count=0)。历史正常供数过
  (10-16 15:43:13 日志有 429 限流记录 = 端点能被供数; 库里 10-12~10-19
  每日 7~25 行正常量) ⇒ 2003 是**当下供数状态** (权限/端点下线/参数
  不适用), **不是**「历史日期查不到」— 处置含义不同: 前者等源恢复,
  后者无从回填。
⇒ 刀5 前提: 缺口当前不可回填; 脚本价值 = 「源恢复那天一条命令就能补」。
⇒ 13 个「意外多出来的缺口日」(08-13/14/17/19/20/24/25, 09-04/08/10/
   15/17, 10-16) 在源恢复前**无法判定真缺口还是当天炸板少** — 不猜,
  等源恢复再判。

缺口实测 (生产库 mode=ro, 2026-10-20):
逐日 break_pool vs daily_klines 对拍:
- 09-28=11 / 09-29=8 (已自愈低量日, 炸板池本量就小)
- 09-30=**0** / klines 5572 → **缺口日**
- 10-01~10-07 全 0 / klines 0 → 真假日
- 10-08=**0** / klines 5572 → **缺口日** (10-08 是交易日! 静态日历表
  _holidays 把它列进假日, 但库内 kline 事实 5572 行 — 静态日历与库内
  事实分歧实例; 本脚本回填锚用 kline 事实, 不以静态日历为准)
- 10-09=13 / 10-12=25 (present)
⇒ 库里为 0 的交易日 = 09-30 与 10-08 **加** 08-13 起 13 个候选
   (共 15 个, 源恢复前不判定真假缺口)。
⇒ 09-28/29 非缺口 (已有数据, 脚本标 already_present 不动)。

目标日期选择逻辑 (统筹三条要求, 不硬编码日期):
「daily_klines 当日有行 **且** limit_break_pool 当日行数=0」—
下一同类缺口 (源某天没落库) 自动被同脚本覆盖, 不必再开新卡;
库里已有数据的日期 → skipped already_present, **绝不重拉覆盖**
(同值覆盖虽幂等, 但消耗源配额 + 源侧重放/口径微差会把已正确数据
改成新的 — 那不是回填是覆盖)。
锚用 daily_klines 而非 TradingCalendar: 回填脚本的目标选择该依赖
**库内事实** (kline 在=当日真产出); 与刀1 假日守卫用静态日历恰相反 —
那里判据不能依赖病源库 (库停更=病源), 这里回填是「补库内事实的洞」,
库内事实就是最硬的锚。日历分歧 (kline 有行但日历判非交易日) 仍标注
calendar_divergence, 不静默。

设计:
1. dry-run 默认: 只拉源数+对比, 不写库; --apply 才写 (授权链要素)。
2. 只写 limit_break_pool 一张表 (INSERT OR REPLACE 幂等, PK=(date,code)),
   不碰其他表 — 回填脚本的爆炸半径最小化。
3. 源返空的日期 → 记 skipped, **不写 0 行假快照** (假绿比缺口更坏)。
4. 落库字段口径与 shortterm_collect._collect_limit_break 逐位一致
   (date/code/name/last_price/change_pct/open_times/turnover_ratio/turnover)。
5. 汇总 jsonl (候选/回填/跳过/源空/日历分歧逐日期留痕)。
6. 探测起点 --from 参数化 (默认 = 库内 break_pool 最早日期前 5 天,
   不写死任何具体日)。

源恢复判据 (统筹三条之②, 可判定):
**同一天同一端点, dated 查询返 rows>0 而库里该日仍为 0** ⇒ 源恢复且
该日待补。比「我看到有数据了」可判定 — 一条 dry-run 命令即可判:
  python3 scripts/backfill_break_pool.py   # 候选日出现 rows>0 即信号

用法:
  python3 scripts/backfill_break_pool.py                      # dry-run 自动探测
  python3 scripts/backfill_break_pool.py --dates 2026-09-30   # 指定日期
  python3 scripts/backfill_break_pool.py --from 2026-09-01    # 指定探测起点
  python3 scripts/backfill_break_pool.py --dates 2026-09-30 --apply  # 写库
"""
from __future__ import annotations

import os

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DB_PATH = os.path.expanduser("~/qs/data/qs.db")

# 落库字段口径 — 与 shortterm_collect._collect_limit_break 逐位一致
INSERT_SQL = (
    "INSERT OR REPLACE INTO limit_break_pool "
    "(date, code, name, last_price, change_pct, open_times, "
    "turnover_ratio, turnover) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def _code_from_row(r: dict) -> str:
    return r.get("ticker") or (r.get("thscode", "") or "").split(".")[0]


def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _default_start(conn) -> str:
    """默认探测起点 = 库内 break_pool 最早日期前 5 天 (不写死具体日)。"""
    row = conn.execute("SELECT MIN(date) FROM limit_break_pool").fetchone()
    if row and row[0]:
        try:
            return (date.fromisoformat(row[0])
                    - timedelta(days=5)).isoformat()
        except ValueError:
            pass
    return "2026-01-01"


def _candidate_dates(conn, start: str, end: str) -> tuple[list[str], dict[str, str]]:
    """候选回填日期 = break_pool 为 0 且 daily_klines 当日有行。

    返回 (dates, notes) — notes 记日历分歧 (kline 有行但日历判非交易日,
    如 10-08: 静态日历表把它列进假日, 库内 kline 事实 5572 行 — 以 kline
    为准纳入候选, 标注不静默)。
    库里已有数据的日期不入候选 (already_present 语义 — 绝不重拉覆盖)。
    """
    rows = conn.execute(
        "SELECT date, COUNT(*) FROM daily_klines WHERE date BETWEEN ? AND ? "
        "GROUP BY date", (start, end)).fetchall()
    kline_days = {r[0]: r[1] for r in rows}
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM limit_break_pool WHERE date BETWEEN ? AND ?",
        (start, end)).fetchall()}
    notes: dict[str, str] = {}
    try:
        from common.calendar import TradingCalendar
        cal = TradingCalendar()
    except Exception:
        cal = None
    out = []
    for d, n_kline in sorted(kline_days.items()):
        if n_kline <= 0 or d in have:
            continue
        out.append(d)
        if cal is not None and not cal.is_trading_day(d):
            notes[d] = ("calendar_divergence: kline 有行但日历判非交易日 "
                        "(静态日历与库内事实分歧, 如 10-08) — 以 kline 事实"
                        "为准纳入候选")
    return out, notes


def fetch_date(client, d: str) -> tuple[list[dict], str]:
    """拉某日炸板池 → (归一化行, 备注)。空 → ([], reason)。"""
    rows = client.get_limit_break_pool(d)
    if not rows:
        return [], "source empty"
    out = []
    for r in rows:
        code = _code_from_row(r)
        if not code:
            continue
        out.append({
            "date": d, "code": code, "name": r.get("name", ""),
            "last_price": _safe_float(r.get("last_price")),
            "change_pct": _safe_float(r.get("price_change_ratio_pct")),
            "open_times": int(r.get("open_times") or 0),
            "turnover_ratio": _safe_float(r.get("turnover_ratio_pct")),
            "turnover": _safe_float(r.get("turnover")),
        })
    return out, "ok"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="limit_break_pool 缺口回填 (dry-run 默认)")
    ap.add_argument("--dates", nargs="*", default=None,
                    help="回填日期列表 (默认=自动探测: kline 有行且 break_pool 0 行)")
    ap.add_argument("--from", dest="from_date", default=None,
                    help="自动探测起点 (默认=库内 break_pool 最早日期前 5 天, 不写死)")
    ap.add_argument("--apply", action="store_true",
                    help="实际写库 (缺省 dry-run 只探测; 授权链要素)")
    args = ap.parse_args()

    from data_layer.sources.fuyao_client import FuyaoClient

    summary = {"mode": "apply" if args.apply else "dry-run",
               "dates": {}, "written_total": 0}
    notes: dict[str, str] = {}
    already: dict[str, int] = {}

    if args.dates:
        targets = list(args.dates)
    else:
        end = date.today().isoformat()
        conn0 = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
        try:
            start = args.from_date or _default_start(conn0)
            targets, notes = _candidate_dates(conn0, start, end)
            # already_present 报告: 探测窗内已有数据的日期逐日显形
            # (运维可见「没被漏掉也没被覆盖」, 不静默跳过)。
            kdays = {r[0] for r in conn0.execute(
                "SELECT DISTINCT date FROM daily_klines WHERE date BETWEEN ? AND ?",
                (start, end)).fetchall()}
            present = dict(conn0.execute(
                "SELECT date, COUNT(*) FROM limit_break_pool "
                "WHERE date BETWEEN ? AND ? GROUP BY date",
                (start, end)).fetchall())
            for d in sorted(kdays & set(present)):
                already[d] = present[d]
        finally:
            conn0.close()
        print(f"自动探测 {start}~{end}: 候选 {len(targets)} 日 -> {targets}")
        for d, n in notes.items():
            print(f"  注: {d} {n}")
        if already:
            print(f"  (已有数据不覆盖: {len(already)} 日 "
                  f"{ {k: v for k, v in list(already.items())[:6]} }...)")
            summary["already_present"] = already

    client = FuyaoClient()

    for d in targets:
        if not args.dates and d in notes:
            summary["dates"][d] = {"note": notes[d]}
        try:
            rows, note = fetch_date(client, d)
        except Exception as e:
            summary["dates"][d] = {"error": f"{type(e).__name__}: {e}", "rows": 0}
            print(f"{d}: source error {type(e).__name__}: {e}")
            continue
        if not rows:
            entry = summary["dates"].setdefault(d, {})
            entry.update({"skipped": note, "rows": 0})
            print(f"{d}: skipped ({note}, 不写假快照)")
            continue
        entry = summary["dates"].setdefault(d, {})
        entry.update({"rows": len(rows), "codes": [r["code"] for r in rows[:20]]})
        if args.apply:
            conn = sqlite3.connect(DB_PATH, timeout=30)
            try:
                for r in rows:
                    conn.execute(INSERT_SQL, (
                        r["date"], r["code"], r["name"], r["last_price"],
                        r["change_pct"], r["open_times"],
                        r["turnover_ratio"], r["turnover"]))
                conn.commit()
                entry["written"] = len(rows)
                summary["written_total"] += len(rows)
                print(f"{d}: WRITTEN {len(rows)} rows")
            finally:
                conn.close()
        else:
            print(f"{d}: dry-run {len(rows)} rows (不写; --apply 才落库)")

    print("\n--- summary ---")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.apply:
        print("\n[dry-run] 未写任何库。授权链要素齐备后: --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

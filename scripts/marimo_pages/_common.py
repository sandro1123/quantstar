"""Marimo 页共享模块（a 轨，Phase 2）。

纪律：
  - 读库一律 file:...?mode=ro 短连接；大表 SQL 先聚合，原始行不进内存；
  - 铁律 #12 呈现侧：每页水印 = 生成时间 + 数据截至日 + 陈旧项标灰，不得无声展示旧数；
  - pandas 3.x API（marimo venv 内 3.0.5），禁用 DataFrame.append 等旧 API。
页文件在首格 `sys.path.insert(0, 本目录)` 后 `import _common as C`。
"""
import datetime as _dt
import os
import re
import sqlite3

import pandas as pd

QS = os.environ.get("QS_ROOT", os.path.expanduser("~/qs"))  # 生产路径可配 (CI/测试注入)
DB_URI = f"file:{QS}/data/qs.db?mode=ro"

# #76 周期窗口：页查询时间窗下限。daily 维持页内既有窗口；weekly/monthly
# 由编排器经 MARIMO_REPORT_PERIOD 环境变量注入 export 子进程，页读此变量。
_PERIOD_MIN_DAYS = {"daily": 0, "weekly": 7, "monthly": 30}
_PERIOD_ENV = "MARIMO_REPORT_PERIOD"

# T1 同款 14 作业白名单（与 build_ops_bitable.py 一致）
JOBS = [
    "morning_brief", "pre_market_refresh", "auction_parse", "morning_review",
    "midday_brief", "daily_data_pipeline", "factor_update", "ic_feedback",
    "quality_check", "freshness_audit", "daily_backtest", "evening_report",
    "daily_ops_report", "paper_trading",
]

# 缺口清单：建了没通电的表 →（原因, 卡号）。P4 页渲染时实测行数，不伪造。
GAP_TABLES = {
    "paper_trading": ("纸面交易零记录（仅 paper_trading_stats 有 1 行快照）", "paper_trading 验收卡"),
    "paper_trading_weights": ("纸面交易权重未接线", "paper_trading 验收卡"),
    "backtest_runs": ("daily_backtest 每日在跑但零运行记录落库", "Q-待立"),
    "strategy_runs": ("同上", "Q-待立"),
    "strategy_params": ("同上", "Q-待立"),
    "signal_provenance": ("信号溯源未接线", "Q-待立"),
    "metric_baselines": ("指标基线未建", "Q-待立"),
    "active_factor_weights": ("生效权重快照未接线", "Q-待立"),
    "source_switch_records": ("数据源切换空转环", "并入 Q-15"),
    "action_log": ("执行动作日志未接线", "Q-待立"),
    "schema_quarantine": ("schema 隔离区未启用", "Q-待立"),
}


def _ic_lag_trading_days() -> int:
    """factor_ic 的**合法滞后交易日数** = strategy.ic_horizon（设计: calc_date = T−horizon）。

    IC 须等 horizon 日前向收益兑现才可算（源码锚 src/ops_layer/orchestrator.py:2292
    _resolve_ic_target_date）。读 config/local.yaml；不可读时回落**代码默认 5**
    （保守方向 = 宁可报，不可漏 —— 防假绿优先于防假红）。
    """
    try:
        with open(f"{QS}/config/local.yaml", errors="replace") as f:
            m = re.search(r"^\s*ic_horizon:\s*(\d+)", f.read(), re.M)
        if m:
            return max(int(m.group(1)), 0)
    except Exception:
        pass
    return 5  # 代码默认（orchestrator._cfg_all("strategy.ic_horizon", 5)）


def _trading_days_back(asof: str, n: int) -> _dt.date | None:
    """asof 回退 n 个**交易日**（非自然日）。n<=0 返回 asof 当日；不可解析返回 None。"""
    try:
        d0 = _dt.date.fromisoformat(str(asof)[:10])
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return d0
    try:
        import sys
        if f"{QS}/src" not in sys.path:
            sys.path.insert(0, f"{QS}/src")
        from common.calendar import TradingCalendar
        cal = TradingCalendar(db=None)
        s = d0.isoformat()
        for _ in range(n):
            s = cal.prev_trading_day(s)
        return _dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None  # 日历不可用 → 判不了, 由调用方保守处理


# 陈旧监视：表 →（日期列, 备注[, 合法滞后交易日数]）。
# 第三项 = 设计允许的滞后（默认 0 = 当日到位）。**带滞后项的表不得按 asof 当日判**
# （否则恒假红；同族先例 src/ops_layer/readiness.py:29-30 adjust_factors 不入清单，
# 「需时点感知期望值」）。期望日 = asof 回退 N 个交易日。
_LAG_FACTOR_IC = _ic_lag_trading_days()
STALE_WATCH = {
    "index_daily": ("date", "Q-51 卡在办"),
    "lhb_daily": ("date", "停滞待查"),
    "sector_fund_flow": ("date", "停滞待查"),
    # factor_ic 设计滞后 T−ic_horizon 交易日（09-16 F席 E6 定谳：非停摆）。
    # 旧备注「实测停 08-12」是 07-14~08-11 断档期的快照，已过期且是假红。
    "factor_ic": ("calc_date",
                  f"ic_feedback 设计滞后 T−{_LAG_FACTOR_IC} 交易日（strategy.ic_horizon）",
                  _LAG_FACTOR_IC),
}


def report_period() -> str:
    """#76：当前报告周期（daily/weekly/monthly），未知/未设一律回落 daily。"""
    _p = os.environ.get(_PERIOD_ENV, "daily").strip().lower()
    return _p if _p in _PERIOD_MIN_DAYS else "daily"


def window_days(base_days: int, period: str = None) -> int:
    """#76：周期感知时间窗。规则（对统筹承诺口径）：
      - daily（默认）：原样返回 base_days，页内既有窗口零变化（向后兼容）；
      - weekly：base_days < 7 抬到 7，>=7 不动；
      - monthly：base_days < 30 抬到 30，>=30 不动。
    未知周期按 daily 处理（window_days(7, 'bogus') == 7）。"""
    p = (period or report_period())
    floor = _PERIOD_MIN_DAYS.get(p, 0)
    return max(base_days, floor)


def ro_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_URI, uri=True, timeout=10)


def bj_now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    with ro_conn() as c:
        return pd.read_sql_query(sql, c, params=params)


def one(sql: str, params: tuple = ()) -> tuple:
    with ro_conn() as c:
        row = c.execute(sql, params).fetchone()
    return row if row else ()


def data_asof() -> str:
    """数据截至日 = 行情数据日（非心跳日）。

    P1-3 (2026-09-28): 原实现从 MAX(date) FROM pipeline_runs（全库心跳日）取——
    这是「系统什么时候跑过」而不是「行情数据到哪天」。
    09-25 实证: pipeline_runs 有行（管道跑了）但 daily_klines 0 行（行情没到）
    → data_asof 返回 09-25（心跳日）但行情数据实际只到 09-24。
    判例「判异常前必核该表生产语义」: pipeline_runs.date 是「管道运行日」≠「数据业务日」。

    四级降级链 (09-28 统筹裁 c 两案合一, sm-6025654604c345fe):
      ① stock_pool.last_price_date — 真吃到价才算 (执行层持仓最新报价日)
         7c8c9004 只到 daily_klines 一级, 「数据到但价没到」场景失真 (09-25
         数据到但价停更) — 故 last_price_date 提到最高优先级。
      ② daily_klines.date — 数据到才算 (行情表最新日)
      ③ stock_pool.added_date — 业务日兜底 (行情表空时仍有业务语义)
      ④ "unknown" — 三锚皆无 (宁 unknown 不假锚)
    """
    row = one("SELECT MAX(last_price_date) FROM stock_pool "
              "WHERE last_price_date IS NOT NULL")
    if row and row[0]:
        return row[0]
    row = one("SELECT MAX(date) FROM daily_klines")
    if row and row[0]:
        return row[0]
    row = one("SELECT MAX(added_date) FROM stock_pool")
    return row[0] if row and row[0] else "unknown"


def expected_lag_days(table: str) -> int:
    """表的**合法滞后交易日数**（未声明 → 0）。单点真源, 供页文案与判据共用。"""
    spec = STALE_WATCH.get(table)
    if spec is None:
        return 0
    return int(spec[2]) if len(spec) > 2 else 0


def expected_floor(table: str, asof: str) -> _dt.date | None:
    """表的**期望最新日** = asof 回退该表声明的合法滞后交易日数。

    单点真源 (F席 09-16): staleness_report 与各页 (p2_factors) 共用, 防两处
    各写一份判定再漂移 (旧 p2 页自行 `str(IC_DATE) < ASOF` 裸比 → 同源假红)。
    未知表 / asof 不可解析 / 日历不可用 → None (调用方保守处理, 不得静默当不陈旧)。
    """
    spec = STALE_WATCH.get(table)
    if spec is None:
        return None
    lag = int(spec[2]) if len(spec) > 2 else 0
    if lag <= 0:
        try:
            return _dt.date.fromisoformat(str(asof)[:10])
        except (TypeError, ValueError):
            return None
    return _trading_days_back(asof, lag)


def staleness_report(asof: str) -> list:
    """返回 ['表名 停在 YYYY-MM-DD（备注）']，仅报落后于**期望日**的表。

    期望日 = asof 回退该表声明的合法滞后交易日数（默认 0 = 当日到位）。
    带滞后的表（如 factor_ic 的 T−ic_horizon）不得按 asof 当日判 —— 否则恒假红。
    """
    out = []
    try:
        _dt.date.fromisoformat(asof)
    except (TypeError, ValueError):
        return [f"{t}（截至日 {asof} 不可解析，无法判定）" for t in STALE_WATCH]
    for table, spec in STALE_WATCH.items():
        col, note = spec[0], spec[1]
        lag = int(spec[2]) if len(spec) > 2 else 0
        try:
            row = one(f"SELECT MAX({col}) FROM {table}")
        except Exception as e:
            out.append(f"{table} 查询失败: {e}")
            continue
        mx = row[0] if row else None
        if not mx:
            out.append(f"{table} 空表（{note}）")
            continue
        try:
            mx_d = _dt.date.fromisoformat(str(mx)[:10])
        except ValueError:
            out.append(f"{table} 日期不可解析: {mx}（{note}）")
            continue
        if lag > 0:
            expected = expected_floor(table, asof)
            if expected is None:
                # 日历不可用 → 判不了, 不静默放过 (保守: 按当日口径报, 提示人工核)
                out.append(f"{table} 期望日不可算（滞后 {lag} 交易日, 日历不可用; {note}）")
                continue
            if mx_d < expected:
                out.append(f"{table} 停在 {str(mx)[:10]}（滞后超 T−{lag}; {note}）")
        elif mx_d < expected_floor(table, asof):
            out.append(f"{table} 停在 {str(mx)[:10]}（{note}）")
    return out


def watermark_html(asof: str, stale: list) -> str:
    """铁律 #12 水印：生成时间 + 数据截至日 + 陈旧项标灰。"""
    stale_html = ""
    if stale:
        items = "".join(f"<li>{s}</li>" for s in stale)
        stale_html = (
            '<div style="margin-top:6px;color:#8a919f;font-size:12px;">'
            f"⚠️ 陈旧项（早于数据截至日，结论谨慎引用）：<ul style='margin:4px 0 0 16px;'>"
            f"{items}</ul></div>")
    p = report_period()
    period_label = {"weekly": "周报", "monthly": "月报"}.get(p, "日报")
    return (
        '<div style="border:1px solid #d0d7de;border-left:4px solid #0969da;'
        'border-radius:6px;padding:10px 14px;background:#f6f8fa;margin:8px 0 16px;">'
        f'<div style="font-size:13px;color:#57606a;">🕒 生成 {bj_now()} ｜ 数据截至 <b>{asof}</b>'
        f" ｜ 报告口径 <b>{period_label}</b>"
        " ｜ 静态导出（marimo），只读聚合生成，无实时连接</div>"
        f"{stale_html}</div>")


# 健康值中文化 + 数据可信度人话提示 (TASK_20260921_01 定谳: 运维一行保留,
# 措辞向「数据可信度」调档, 让用户一眼判断今日推荐成色)
_HEALTH_CN = {"green": "绿色", "yellow": "黄色", "red": "红色"}
_HEALTH_HINT = {
    "green": "管道全绿，今日推荐依据完整",
    "yellow": "有管道异常，相关推荐信心关注",
    "red": "数据链有失败腿，今日推荐信心打折",
}


def ops_oneliner() -> str:
    """运维一行：最新日运行计数 + 数据可信度(中文) + 未解决告警。"""
    asof = data_asof()
    row = one("""
        SELECT COUNT(*), SUM(status='success'), SUM(status='failed'),
               SUM(status='skipped')
        FROM pipeline_runs WHERE date = ?""", (asof,))
    n, s, f_, k = (row + (0, 0, 0, 0))[:4]
    health_row = one(
        "SELECT overall_status FROM health_history ORDER BY check_time DESC LIMIT 1")
    health = health_row[0] if health_row else "unknown"
    open_alerts = one("SELECT COUNT(*) FROM alerts WHERE status='open'")[0]
    line = (f"{asof} 运行 {n}（✅{s or 0} ❌{f_ or 0} ⏭{k or 0}）"
            f"｜ 数据可信度 {_HEALTH_CN.get(health, '未知')} ｜ 未决告警 {open_alerts}")
    hint = _HEALTH_HINT.get(health, "")
    return f"{line}——{hint}" if hint else line


def latest_signals() -> pd.DataFrame:
    """最新信号日 pending_entry 行动卡（JOIN 最新审批状态），无信号返回空 df。"""
    row = one("""
        SELECT MAX(added_date) FROM stock_pool
        WHERE status='pending_entry' AND added_date >= date('now','-14 day')""")
    target = row[0] if row else None
    if not target:
        return pd.DataFrame(columns=[
            "rank", "code", "name", "composite_score", "buy_range",
            "stop_loss", "one_line_reason", "review_status"])
    df = fetch_df("""
        SELECT code, name, initial_score AS composite_score,
               entry_low, entry_high, stop_loss, added_reason
        FROM stock_pool
        WHERE status='pending_entry' AND added_date = ?
        ORDER BY initial_score DESC""", (target,))
    hrq = dict(fetch_df("""
        SELECT code, status FROM human_review_queue hrq
        WHERE created_at = (SELECT MAX(created_at) FROM human_review_queue
                            WHERE code = hrq.code)""").values.tolist())
    df = df.copy()
    df.insert(0, "rank", range(1, len(df) + 1))
    df["buy_range"] = df.apply(
        lambda r: (f"{r['entry_low']}~{r['entry_high']}"
                   if pd.notna(r["entry_low"]) and pd.notna(r["entry_high"]) else ""), axis=1)
    df["stop_loss"] = df["stop_loss"].where(df["stop_loss"].notna(), "")
    # Day 3 卡一: dashboard 理由位 切 _pick_reason_card (与 晨报 + DM 卡同源)。
    # rationale 表 0 行 → 回落 _pick_reason 单句; 有 rationale 时升级为多字段论证卡。
    # conn 用 ro_conn() 短开 (与 latest_signals 内部一致),不依赖 execution_layer。
    try:
        from report_reason import _pick_reason_card  # noqa: F401
    except ImportError:  # marimo subprocess 环境缺包 时 回落 旧版切片
        df["one_line_reason"] = df["added_reason"].fillna("").str[:120]
    else:
        try:
            with ro_conn() as _cc:
                df["one_line_reason"] = df.apply(
                    # 判例#132: 补 code 锚 (Day3 验收实证 920394 真实路径)
                    lambda r: _pick_reason_card(
                        {"code": r.get("code", ""), "added_reason": r["added_reason"]},
                        conn=_cc,
                        date=r.get("signal_date") or "")[:120],
                    axis=1)
        except Exception:
            # 回落 旧版切片 (不 让 dashboard render 失败)
            df["one_line_reason"] = df["added_reason"].fillna("").str[:120]
    df["review_status"] = df["code"].map(hrq).fillna("not_queued")
    df["signal_date"] = target
    return df.drop(columns=["entry_low", "entry_high", "added_reason"])

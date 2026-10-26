"""advisory 命令组 — 投顾类只读查询 (MVP P1)

设计稿: 分层讨论/投顾CLI与quantmaster集成设计稿-E-20260828.md
只读三重保障:
  ① sqlite mode=ro URI 物理只读 (写操作抛 OperationalError)
  ② 不 bootstrap (不初始化 delivery/alert/execution 层)
  ③ 不联网 (全走本地 qs.db, 无 requests/akshare)
输出契约: --json 稳定 schema (schema_version=1, 键英文值可中文, *_zh 成品中文);
人类可读输出全中文。失败诚实: ok:false + error.code + 非零 exit, 绝不返回假值。
"""
import json
import os
import sqlite3
import time

import click

SCHEMA_VERSION = 1

def _resolve_db_path() -> str:
    """库路径解析 — 与 common.database.Database 默认逻辑一致
    (QS_TEST_DB 测试隔离变量优先, 生产从不设置)。"""
    return os.environ.get("QS_TEST_DB") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "qs.db")

def _today() -> str:
    """当前北京日期 (YYYY-MM-DD) — 独立函数便于测试注入锁死, 防日期漂移。"""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")

def _ro_connect() -> sqlite3.Connection:
    """只读连接 (保障①): mode=ro 物理杜绝写; timeout 2s 防与生产写撞锁。"""
    conn = sqlite3.connect(f"file:{_resolve_db_path()}?mode=ro", uri=True,
                           timeout=2, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _envelope(command: str, started: float, **payload) -> dict:
    """统一输出契约: schema_version/ok/data_asof/command/elapsed_ms"""
    return {"schema_version": SCHEMA_VERSION, "ok": True,
            "command": command,
            "elapsed_ms": int((time.time() - started) * 1000),
            **payload}

def _error(command: str, started: float, code: str, message_zh: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "ok": False,
            "command": command,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": {"code": code, "message_zh": message_zh}}

def _emit(ctx, data: dict, exit_code: int = 0):
    """输出: --json 原样 JSON; 否则人类可读中文 (由各命令塞 _human 渲染)。"""
    if ctx.obj.get("json"):
        click.echo(json.dumps(data, ensure_ascii=False))
    else:
        human = data.pop("_human", None)
        click.echo(human if human else json.dumps(data, ensure_ascii=False))
    if exit_code:
        raise SystemExit(exit_code)

@click.group()
def advisory():
    """投顾查询 (只读) — 个股分析/建议/候选/持仓等, 供用户与 quantmaster 调用"""

@advisory.command()
@click.argument("query")
@click.pass_context
def resolve(ctx, query):
    """名称/代码 → 股票代码 (模糊匹配, NL 接入前置)"""
    started = time.time()
    cmd = "advisory resolve"
    q = query.strip()
    try:
        conn = _ro_connect()
    except sqlite3.OperationalError as e:
        _emit(ctx, _error(cmd, started, "DB_ERROR", f"数据库连接失败: {e}"), 1)
        return
    try:
        # 精确代码优先; 否则名称模糊匹配 (is_active 优先排序, 退市股排后)
        rows = conn.execute(
            "SELECT code, name, industry, is_active, delist_date FROM stock_info "
            "WHERE code = ?", (q,)).fetchall()
        if not rows:
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT code, name, industry, is_active, delist_date FROM stock_info "
                "WHERE name LIKE ? ORDER BY is_active DESC, code LIMIT 20",
                (like,)).fetchall()
        if not rows:
            _emit(ctx, _error(cmd, started, "STOCK_NOT_FOUND",
                              f"未找到与「{q}」匹配的股票"), 1)
            return
        matches = [{"code": r["code"], "name": r["name"],
                    "industry": r["industry"] or "",
                    "is_active": int(r["is_active"] or 0),
                    "delist_date": r["delist_date"] or ""} for r in rows]
        data = _envelope(cmd, started, data_asof="", matches=matches)
        lines = [f"🔍 「{q}」匹配到 {len(matches)} 只:"]
        for m in matches:
            flag = "" if m["is_active"] else f" ⚠️已退市({m['delist_date']})"
            lines.append(f"  {m['code']} {m['name']} [{m['industry']}]{flag}")
        data["_human"] = "\n".join(lines)
        _emit(ctx, data)
    finally:
        conn.close()

# N3 (2026-09-14): factor_scores JSON 键 → 输出键映射 (89 因子体系改名表)
# 物理列名 (advisory 输出契约)  →  JSON 键名 (87 键体系)
# 注: momentum_5d→momentum_5d_reversal 与 volatility_20d→volatility_reversal
# 是语义反转因子 (反转=低动量得分高), 输出键名保持物理列名不变 (消费方兼容),
# 语义注记在 note_zh (不另立键, 保持 schema_version=1 稳定)。
_JSON_TECH_KEYS = {
    "momentum_5d": "momentum_5d_reversal",
    "momentum_20d": "momentum_20d",
    "volatility_20d": "volatility_reversal",
    "volume_ratio": "turnover_ratio",
    "rsi_14": "rsi14",
}

_REGIME_ZH = {"bull": "偏多", "bear": "偏空", "neutral": "中性"}

@advisory.command()
@click.argument("code")
@click.pass_context
def analyze(ctx, code):
    """个股综合分析 — 评分/估值/技术面/池状态/消息/大盘 (只读)"""
    started = time.time()
    cmd = "advisory analyze"
    code = code.strip()
    try:
        conn = _ro_connect()
    except sqlite3.OperationalError as e:
        _emit(ctx, _error(cmd, started, "DB_ERROR", f"数据库连接失败: {e}"), 1)
        return
    try:
        info = conn.execute(
            "SELECT code, name, industry FROM stock_info WHERE code = ?",
            (code,)).fetchone()
        if not info:
            _emit(ctx, _error(cmd, started, "STOCK_NOT_FOUND",
                              f"未找到股票 {code}"), 1)
            return
        today = _today()
        asof_dates = []
        notes = []

        # 评分 (model_predictions 最新一条)
        score = None
        row = conn.execute(
            "SELECT date, ml_score, factor_score, blended_score FROM model_predictions "
            "WHERE code = ? ORDER BY date DESC LIMIT 1", (code,)).fetchone()
        if row:
            asof_dates.append(row["date"])
            score = {"blended": row["blended_score"], "ml": row["ml_score"],
                     "factor": row["factor_score"], "date": row["date"]}
        else:
            notes.append("暂无模型评分数据")

        # 估值 + 技术面 (N3 2026-09-14 换代修): 89 因子体系下 factor_daily 只写
        # factor_scores JSON 列, 物理因子列 (pe_ttm/momentum_5d/rsi_14...) 自
        # 2026-07-21 起零写入 (聚合器 20→89 因子换代只写 JSON, 未查旧消费面 —
        # 侦察档 N3)。旧读法恒 NULL → 用户面静默坏。
        # 修法: 技术面读 factor_scores JSON (键名映射见 _JSON_TECH_KEYS);
        # 估值面三键 (pe_ttm/pb/roe) JSON 里无原始值 (89 体系是 value_discount
        # 标准化形态), 切 stock_valuation 当日真值源。向后兼容: 老库 JSON 列
        # 空/缺列时回落物理列 (07-21 前老数据仍可读)。
        valuation = None
        technicals = None
        factor_date = None
        json_scores = None
        fd_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(factor_daily)")}
        sel_cols = ["date", "pe_ttm", "pb", "roe", "momentum_5d", "momentum_20d",
                    "volatility_20d", "volume_ratio", "rsi_14"]
        if "factor_scores" in fd_cols:
            sel_cols.append("factor_scores")  # 新形态库 (07-21 后) 才有 JSON 列
        row = conn.execute(
            "SELECT " + ", ".join(sel_cols) + " FROM factor_daily "
            "WHERE code = ? ORDER BY date DESC LIMIT 1", (code,)).fetchone()
        if row:
            factor_date = row["date"]
            asof_dates.append(row["date"])
            if "factor_scores" in row.keys() and row["factor_scores"]:
                try:
                    json_scores = json.loads(row["factor_scores"])
                except (ValueError, TypeError):
                    json_scores = None  # JSON 损坏 → 回落物理列, 不造假
            if json_scores:
                technicals = {k: json_scores.get(jk)
                             for k, jk in _JSON_TECH_KEYS.items()}
                if all(v is None for v in technicals.values()):
                    technicals = None  # JSON 有但键全缺 → 视为无技术面
            if json_scores is None:  # 老形态 (07-21 前) 或 JSON 损坏 → 物理列
                technicals = {"momentum_5d": row["momentum_5d"],
                              "momentum_20d": row["momentum_20d"],
                              "volatility_20d": row["volatility_20d"],
                              "volume_ratio": row["volume_ratio"],
                              "rsi_14": row["rsi_14"]}
                if all(v is None for v in technicals.values()):
                    technicals = None

        # 估值面: stock_valuation 当日真值 (N3 修法 — JSON 里无原始 PE/pb/roe)
        # 缺表防御: 极老库/测试库无 stock_valuation → 回落物理列 (07-21 前形态)
        if factor_date:
            sv_tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_valuation'")}
            if sv_tables:
                row = conn.execute(
                    "SELECT pe_ttm, pb_mrq FROM stock_valuation "
                    "WHERE code = ? AND date = ?", (code, factor_date)).fetchone()
                if row and row["pe_ttm"] is not None:
                    valuation = {"pe_ttm": row["pe_ttm"], "pb": row["pb_mrq"]}
            if valuation is None:
                fd_row = conn.execute(
                    "SELECT pe_ttm, pb FROM factor_daily "
                    "WHERE code = ? AND date = ?", (code, factor_date)).fetchone()
                if fd_row and fd_row["pe_ttm"] is not None:
                    valuation = {"pe_ttm": fd_row["pe_ttm"], "pb": fd_row["pb"]}

        if factor_date is None:
            notes.append("暂无因子数据")
        if valuation is None:
            notes.append("暂无估值数据")
        if technicals is None:
            notes.append("暂无技术面数据")

        # 池状态
        pool_status = {"in_pool": False, "status": None, "entry_range": None,
                       "stop_loss": None}
        row = conn.execute(
            "SELECT status, entry_low, entry_high, stop_loss FROM stock_pool "
            "WHERE code = ?", (code,)).fetchone()
        if row:
            pool_status = {"in_pool": True, "status": row["status"],
                           "entry_range": [row["entry_low"], row["entry_high"]],
                           "stop_loss": row["stop_loss"]}

        # 近期消息 (最多 5 条)
        news = [{"date": r["date"], "title": r["title"]} for r in conn.execute(
            "SELECT date, title FROM stock_news WHERE code = ? "
            "ORDER BY date DESC LIMIT 5", (code,))]

        # 大盘状态 (全市场背景, 不计入个股新鲜度 — stale 只看评分/因子)
        regime = {"market": None, "market_zh": ""}
        row = conn.execute(
            "SELECT date, regime FROM daily_regime ORDER BY date DESC LIMIT 1").fetchone()
        if row:
            regime = {"market": row["regime"],
                      "market_zh": _REGIME_ZH.get(row["regime"], row["regime"] or "")}

        # 新鲜度: 实际用到的数据最大日期 vs 今天
        data_asof = max(asof_dates) if asof_dates else ""
        stale = bool(data_asof and data_asof < today)
        stale_reason = (f"数据截至 {data_asof}, 早于今日 {today}"
                        if stale else "")

        # 中文摘要 (结论先行)
        parts = []
        if score:
            parts.append(f"综合评分 {score['blended']:.1f}")
        if valuation and valuation.get("pe_ttm"):
            parts.append(f"PE(TTM) {valuation['pe_ttm']:.1f}")
        if pool_status["in_pool"]:
            parts.append(f"已在池中({pool_status['status']})")
        if regime["market_zh"]:
            parts.append(f"大盘{regime['market_zh']}")
        summary_zh = (f"{info['name']}：" + "，".join(parts) + "。") if parts else \
            f"{info['name']}：暂无足够数据生成分析。"

        data = _envelope(cmd, started, data_asof=data_asof,
                         code=code, name=info["name"],
                         industry=info["industry"] or "",
                         score=score, valuation=valuation, technicals=technicals,
                         pool_status=pool_status, recent_news=news, regime=regime,
                         stale=stale, stale_reason_zh=stale_reason,
                         note_zh="；".join(notes), summary_zh=summary_zh)
        lines = [f"📊 {info['name']} ({code}) 综合分析",
                 f"  {summary_zh}"]
        if stale:
            lines.append(f"  ⚠️ {stale_reason}")
        if news:
            lines.append("  近期消息:")
            lines.extend(f"    [{n['date']}] {n['title']}" for n in news[:3])
        data["_human"] = "\n".join(lines)
        _emit(ctx, data)
    finally:
        conn.close()
# advise 规则阈值 — 基于相对排名百分位 (生产 blended_score 是原始模型分
# 量级约 -2.3~1.5, 无绝对阈值可用; 百分位跨模型版本稳定)
_ADVISE_HIGH_PCT = 0.80   # 评分前 20% = 高分
_ADVISE_LOW_PCT = 0.30    # 评分后 30% = 低分
_NEAR_STOP_PCT = 0.05     # 现价距止损 5% 内 = 接近止损告警

def _score_percentile(conn, code: str) -> tuple[float | None, str | None]:
    """该股最新日期 blended_score 在全市场的百分位 (0~1) + 数据日期。
    无评分返回 (None, None)。"""
    row = conn.execute(
        "SELECT blended_score, date FROM model_predictions WHERE code = ? "
        "ORDER BY date DESC LIMIT 1", (code,)).fetchone()
    if not row or row["blended_score"] is None:
        return None, None
    score, date = row["blended_score"], row["date"]
    total = conn.execute(
        "SELECT COUNT(*) FROM model_predictions WHERE date = ? "
        "AND blended_score IS NOT NULL", (date,)).fetchone()[0]
    if not total:
        return None, None
    lower = conn.execute(
        "SELECT COUNT(*) FROM model_predictions WHERE date = ? "
        "AND blended_score < ?", (date, score)).fetchone()[0]
    return lower / total, date

_VERDICT_ZH = {"buy": "可买入", "watch": "观望", "avoid": "不建议买入",
               "stop_loss_warning": "注意止损"}

@advisory.command()
@click.argument("code")
@click.pass_context
def advise(ctx, code):
    """投顾建议 — 可买/观望/不建议/注意止损 + 理由 (只读, 确定性规则无 LLM)"""
    started = time.time()
    cmd = "advisory advise"
    code = code.strip()
    try:
        conn = _ro_connect()
    except sqlite3.OperationalError as e:
        _emit(ctx, _error(cmd, started, "DB_ERROR", f"数据库连接失败: {e}"), 1)
        return
    try:
        info = conn.execute(
            "SELECT code, name FROM stock_info WHERE code = ?", (code,)).fetchone()
        if not info:
            _emit(ctx, _error(cmd, started, "STOCK_NOT_FOUND",
                              f"未找到股票 {code}"), 1)
            return
        pct, score_date = _score_percentile(conn, code)
        pool = conn.execute(
            "SELECT status, entry_low, entry_high, stop_loss, take_profit, "
            "last_price, position_qty, dynamic_stop_loss FROM stock_pool "
            "WHERE code = ?", (code,)).fetchone()
        regime = conn.execute(
            "SELECT regime FROM daily_regime ORDER BY date DESC LIMIT 1").fetchone()
        regime_zh = _REGIME_ZH.get(regime["regime"], "") if regime else ""

        held = bool(pool and pool["status"] == "active" and pool["position_qty"])
        reasons, risks = [], []
        verdict = "watch"
        entry_plan = None

        # 规则1 (最高优先): 已持仓且现价触及止损 → stop_loss_warning
        if held:
            stop = pool["dynamic_stop_loss"] or pool["stop_loss"]
            lp = pool["last_price"]
            if stop and lp and lp <= stop:
                verdict = "stop_loss_warning"
                reasons.append(f"现价 {lp} 已触及止损位 {stop}")
            elif stop and lp and (lp - stop) / lp <= _NEAR_STOP_PCT:
                risks.append(f"现价 {lp} 距止损位 {stop} 不足 5%")

        # 规则2: 在池待入场且评分不低 → buy
        if verdict == "watch" and pool and pool["status"] == "pending_entry":
            if pct is None or pct >= _ADVISE_LOW_PCT:
                verdict = "buy"
                entry_plan = {"low": pool["entry_low"], "high": pool["entry_high"]}
                reasons.append("已在池中待入场")
                if pct is not None:
                    reasons.append(f"评分位于全市场前 {int((1 - pct) * 100)}%")
                if pool["entry_low"] and pool["entry_high"]:
                    reasons.append(
                        f"建议在入场区间 {pool['entry_low']}-{pool['entry_high']} 分批介入")

        # 规则3: 高分但不在池 → watch (等信号)
        if verdict == "watch" and pct is not None and pct >= _ADVISE_HIGH_PCT:
            reasons.append(f"评分位于全市场前 {int((1 - pct) * 100)}%, 但尚未入池, 等待信号")

        # 规则4: 低分 → avoid
        if pct is not None and pct <= _ADVISE_LOW_PCT and verdict in ("watch",):
            if not (pool and pool["status"] == "pending_entry"):
                verdict = "avoid"
                reasons.append(f"评分位于全市场后 {int(pct * 100)}%, 动能不足")

        if pct is None:
            reasons.append("暂无模型评分, 无法量化判断")
        if regime_zh:
            risks.append(f"大盘情绪{regime_zh}")

        # confidence: 有评分时按百分位离中位距离给, 无评分给低置信
        confidence = round(abs((pct or 0.5) - 0.5) * 2 * 0.8 + 0.2, 2)

        position_context = {"already_held": held,
                            "portfolio_overlap_pct": 0.0}
        data = _envelope(cmd, started, data_asof=score_date or "",
                         code=code, name=info["name"],
                         verdict=verdict, verdict_zh=_VERDICT_ZH[verdict],
                         confidence=confidence,
                         reasons_zh=reasons, risks_zh=risks,
                         entry_plan=entry_plan,
                         position_context=position_context)
        lines = [f"💡 {info['name']} ({code}) 投顾建议: {_VERDICT_ZH[verdict]}",
                 f"  置信度 {confidence}"]
        lines.extend(f"  · {r}" for r in reasons)
        if risks:
            lines.append("  风险:")
            lines.extend(f"    ⚠️ {r}" for r in risks)
        data["_human"] = "\n".join(lines)
        _emit(ctx, data)
    finally:
        conn.close()

@advisory.command()
@click.option("--top", "top_n", default=10, show_default=True, help="候选数量")
@click.pass_context
def candidates(ctx, top_n):
    """今日候选 — 最新评分 topN + 在池标记 (只读)"""
    started = time.time()
    cmd = "advisory candidates"
    try:
        conn = _ro_connect()
    except sqlite3.OperationalError as e:
        _emit(ctx, _error(cmd, started, "DB_ERROR", f"数据库连接失败: {e}"), 1)
        return
    try:
        latest = conn.execute(
            "SELECT MAX(date) FROM model_predictions").fetchone()[0]
        if not latest:
            _emit(ctx, _error(cmd, started, "DATA_MISSING",
                              "暂无模型评分数据, 无法生成候选"), 1)
            return
        total = conn.execute(
            "SELECT COUNT(*) FROM model_predictions WHERE date = ? "
            "AND blended_score IS NOT NULL", (latest,)).fetchone()[0]
        if not total:
            _emit(ctx, _error(cmd, started, "DATA_MISSING",
                              f"{latest} 无有效评分数据"), 1)
            return
        rows = conn.execute(
            "SELECT p.code, COALESCE(i.name, p.code) AS name, p.blended_score, "
            "CASE WHEN s.code IS NOT NULL THEN 1 ELSE 0 END AS in_pool "
            "FROM model_predictions p "
            "LEFT JOIN stock_info i ON i.code = p.code "
            "LEFT JOIN stock_pool s ON s.code = p.code "
            "WHERE p.date = ? AND p.blended_score IS NOT NULL "
            "ORDER BY p.blended_score DESC LIMIT ?", (latest, top_n)).fetchall()
        cands = [{"code": r["code"], "name": r["name"],
                  "blended_score": r["blended_score"],
                  "rank": i + 1, "in_pool": bool(r["in_pool"])}
                 for i, r in enumerate(rows)]
        regime = conn.execute(
            "SELECT regime FROM daily_regime ORDER BY date DESC LIMIT 1").fetchone()
        regime_zh = (f"大盘{_REGIME_ZH.get(regime['regime'], regime['regime'] or '')}"
                     if regime else "")
        data = _envelope(cmd, started, data_asof=latest,
                         candidates=cands, total_screened=total,
                         regime_zh=regime_zh)
        lines = [f"🎯 今日候选 Top{len(cands)} (评分日期 {latest}, 全市场 {total} 只)"]
        for c in cands:
            pool_flag = " [已在池]" if c["in_pool"] else ""
            lines.append(f"  {c['rank']:>2}. {c['code']} {c['name']} "
                         f"评分 {c['blended_score']:.3f}{pool_flag}")
        if regime_zh:
            lines.append(f"  {regime_zh}")
        data["_human"] = "\n".join(lines)
        _emit(ctx, data)
    finally:
        conn.close()

@advisory.command()
@click.pass_context
def holdings(ctx):
    """推荐跟踪 — stock_pool active 跟踪中 + 待结算单列 (只读)"""
    started = time.time()
    cmd = "advisory holdings"
    try:
        conn = _ro_connect()
    except sqlite3.OperationalError as e:
        _emit(ctx, _error(cmd, started, "DB_ERROR", f"数据库连接失败: {e}"), 1)
        return
    try:
        today = _today()
        positions = []
        for r in conn.execute(
                # polish①: name 从 stock_info join 取 — stock_pool 部分股 name
                # 字段存的是代码, COALESCE(i.name, p.name) 优先真名回退池内值
                "SELECT p.code, COALESCE(i.name, p.name) AS name, p.entry_price, "
                "p.last_price, p.position_qty, p.unrealized_pnl_pct, "
                "p.holding_days, p.stop_loss, p.dynamic_stop_loss, "
                "p.take_profit, p.entry_date FROM stock_pool p "
                "LEFT JOIN stock_info i ON i.code = p.code "
                "WHERE p.status = 'active' ORDER BY p.code"):
            note = ""
            lp = r["last_price"]
            pnl = r["unrealized_pnl_pct"]
            # polish②: 今日入场无新价 — 禁用昨收/成交价冒充现价 (宁可 NULL 不可假值)。
            # 有今日收盘价 (daily_klines) 则用之并现算盈亏; 无则 NULL + note 提示。
            if str(r["entry_date"] or "")[:10] == today:
                krow = conn.execute(
                    "SELECT close FROM daily_klines WHERE code = ? AND date = ?",
                    (r["code"], today)).fetchone()
                if krow and krow["close"] and r["entry_price"]:
                    lp = krow["close"]
                    pnl = round((lp / r["entry_price"] - 1) * 100, 2)
                else:
                    lp = None
                    pnl = None
                    note = "今日入场,暂无最新收盘价,盈亏待结算"
            alert = ""
            stop = r["dynamic_stop_loss"] or r["stop_loss"]
            if stop and lp:
                if lp <= stop:
                    alert = f"⚠️ 现价 {lp} 已触及参考卖出线 {stop}"
                elif (lp - stop) / lp <= _NEAR_STOP_PCT:
                    alert = f"⚠️ 现价 {lp} 距参考卖出线 {stop} 不足 5%, 注意风险"
            positions.append({
                "code": r["code"], "name": r["name"], "status": "active",
                "entry_price": r["entry_price"], "last_price": lp,
                "qty": r["position_qty"],
                "unrealized_pnl_pct": pnl,
                "holding_days": r["holding_days"],
                "stop_loss": r["stop_loss"],
                "dynamic_stop_loss": r["dynamic_stop_loss"],
                "take_profit": r["take_profit"], "alert_zh": alert,
                "note_zh": note})
        pending_exits = [{"code": r["code"], "name": r["name"],
                          "reason_zh": r["exit_reason"] or "待结算"}
                         for r in conn.execute(
                             # polish①: 待结算同样 join 取名
                             "SELECT p.code, COALESCE(i.name, p.name) AS name, "
                             "p.exit_reason FROM stock_pool p "
                             "LEFT JOIN stock_info i ON i.code = p.code "
                             "WHERE p.status = 'exit_pending' ORDER BY p.code")]
        pnls = [p["unrealized_pnl_pct"] for p in positions
                if p["unrealized_pnl_pct"] is not None]
        summary = {"active_count": len(positions),
                   "avg_pnl_pct": round(sum(pnls) / len(pnls), 2) if pnls else None,
                   "total_unrealized_pnl": None}
        data = _envelope(cmd, started, data_asof=_today(),
                         positions=positions, summary=summary,
                         pending_exits=pending_exits)
        if not positions:
            data["_human"] = "📦 当前无跟踪中的推荐。"
            if pending_exits:
                data["_human"] += f"\n  待结算 {len(pending_exits)} 笔。"
            _emit(ctx, data)
            return
        lines = [f"📦 推荐跟踪 {len(positions)} 只"
                 + (f" (平均验证收益 {summary['avg_pnl_pct']}%)"
                    if summary["avg_pnl_pct"] is not None else "")]
        for p in positions:
            pnl = p["unrealized_pnl_pct"]
            pnl_s = f"{pnl:+.1f}%" if pnl is not None else "—"
            lp_s = p["last_price"] if p["last_price"] is not None else "—"
            lines.append(f"  {p['code']} {p['name']} 成本 {p['entry_price']} "
                         f"现价 {lp_s} 验证收益 {pnl_s} 持有 {p['holding_days']} 天")
            if p["note_zh"]:
                lines.append(f"    ℹ️ {p['note_zh']}")
            if p["alert_zh"]:
                lines.append(f"    {p['alert_zh']}")
        if pending_exits:
            lines.append(f"  待结算 {len(pending_exits)} 笔:")
            lines.extend(f"    {e['code']} {e['name']} ({e['reason_zh']})"
                         for e in pending_exits)
        data["_human"] = "\n".join(lines)
        _emit(ctx, data)
    finally:
        conn.close()


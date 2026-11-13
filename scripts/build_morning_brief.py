"""构建真实数据早报，先预览，确认后再推送"""
import sys, os, json, time, re, concurrent.futures
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(project_root)
load_dotenv(os.path.join(project_root, ".env"))

from common.utils import bj_date, bj_now
from common.database import Database
from common.logging import get_logger
from delivery_layer import DeliveryLayer, Message
from report_layer.formatters.feishu_card import FeishuCardFormatter
from config_layer import Config
from data_layer import DataLayer
from data_layer.news_collector import NewsCollector
from agent_layer.llm.service import LLMService
from agent_layer.llm.provider_manager import ProviderManager
from execution_layer import ExecutionLayer
from strategy_layer import StrategyLayer

logger = get_logger("scripts.morning_brief")
DATE = bj_date()
_llm_cache = None


def _readonly_connection():
    import sqlite3
    from pathlib import Path
    # mode=ro 不创建缺失源库，也不走 Database 的 WAL 设置。
    conn = sqlite3.connect(Path(Database().db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _preview_execution_db():
    # 执行层读取方法会建表；只复制持仓表到内存，不复制多 GB 行情库。
    from contextlib import closing
    db = Database(":memory:")
    try:
        with closing(_readonly_connection()) as source:
            source.execute("BEGIN")
            schema = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='stock_pool'").fetchone()
            if not schema:
                raise ValueError("预览源库缺少 stock_pool")
            conn = db.get_connection()
            conn.execute(schema[0])
            rows = source.execute("SELECT * FROM stock_pool")
            columns = ', '.join('"' + c[0].replace('"', '""') + '"' for c in rows.description)
            placeholders = ', '.join('?' for _ in rows.description)
            conn.executemany(f"INSERT INTO stock_pool ({columns}) VALUES ({placeholders})", rows)
            conn.commit()
        return db
    except Exception:
        db.close()
        raise


def _init_llm():
    global _llm_cache
    if _llm_cache is not None:
        return _llm_cache
    try:
        pm = ProviderManager()
        _llm_cache = LLMService(provider_manager=pm)
        return _llm_cache
    except Exception:
        return None


def _llm_call(prompt, timeout_s=20, max_tokens=2000, point="llm"):
    llm = _init_llm()
    if not llm or not llm.is_available():
        return None
    def _do():
        return llm.call(prompt, temperature=0.3, max_tokens=max_tokens, call_point=point)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_do)
            return fut.result(timeout=timeout_s)
    except Exception:
        return None


# ============================================================
# 1. 昨夜外盘
# ============================================================
def build_overnight():
    import requests
    url = "https://hq.sinajs.cn/list=gb_dji,gb_ixic,gb_inx,hf_CL,hf_GC,hf_CHA50CFD"
    try:
        resp = requests.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
        resp.encoding = "gbk"
        data = {}
        stock_map = {"dji": "道琼斯", "ixic": "纳斯达克", "inx": "标普500"}
        fut_map = {"CL": "WTI原油", "GC": "COMEX黄金", "CHA50CFD": "A50期指"}
        for line in resp.text.strip().split("\n"):
            if "=" not in line: continue
            raw = line.split('="')[1].rstrip('"')
            fields = raw.split(",")
            parts = line.split("_")
            symbol = parts[3].split("=")[0] if len(parts) > 3 else ""
            if symbol in stock_map and len(fields) >= 3:
                price = float(fields[1]); pct = float(fields[2].replace("%", ""))
                data[symbol] = {"label": stock_map[symbol], "price": price, "pct": round(pct, 2)}
            elif symbol in fut_map and len(fields) >= 8:
                cur = float(fields[0]); prev = float(fields[7])
                pct = (cur / prev - 1) * 100
                data[symbol] = {"label": fut_map[symbol], "price": round(cur, 1), "pct": round(pct, 2)}
        return data
    except Exception as e:
        logger.warning(f"Overnight failed: {e}")
        return {}


def format_overnight(data):
    if not data: return "数据暂不可用"
    lines = []
    for key in ["dji", "ixic", "inx", "CL", "GC", "CHA50CFD"]:
        if key not in data: continue
        d = data[key]; pct = d["pct"]
        color = "🔴" if pct > 0 else "🟢"; arrow = "↑" if pct > 0 else "↓"
        lines.append(f"{d['label']}: {d['price']:,.0f} {color} {arrow}{abs(pct):.2f}%")
    up = sum(1 for k in ["dji", "ixic", "inx", "CL", "GC", "CHA50CFD"] if k in data and data[k]["pct"] > 0)
    t = sum(1 for k in ["dji", "ixic", "inx", "CL", "GC", "CHA50CFD"] if k in data)
    if t > 0:
        lines.append(f"\n隔夜市场整体{'偏暖' if up > t/2 else '偏冷'}，{up}/{t} 品种上涨")
    return "\n".join(lines)


# ============================================================
# 2. 今日要闻
# ============================================================
def _classify_news(title, summary):
    text = title + " " + summary
    bearish = ["下跌", "利空", "减持", "解禁", "亏损", "违约", "下调", "风险", "加息", "失守", "下降", "跌幅", "跌至"]
    bullish = ["增长", "利好", "突破", "新高", "放量", "政策", "支持", "涨价", "回购", "降息", "签约", "上调", "涨", "成功"]
    score = sum(-1 for k in bearish if k in text) + sum(1 for k in bullish if k in text)
    return "bullish" if score > 0 else "bearish" if score < 0 else "neutral"


def _clean_summary(summary, title):
    s = summary.strip()
    for prefix in [f"【{title[:20]}", f"「{title[:20]}", f"'{title[:20]}"]:
        if s.startswith(prefix):
            end = s.find("】")
            if end > 0:
                s = s[end+1:].strip()
    return s


def build_news(date, dry_run: bool = False):
    try:
        if dry_run:
            from contextlib import closing
            with closing(_readonly_connection()) as conn:
                items = [dict(r) for r in conn.execute(
                    "SELECT code, date, title, source, sentiment, sentiment_score, summary "
                    "FROM stock_news WHERE date LIKE ? ORDER BY date DESC LIMIT 8",
                    (f"{date}%",))]
        else:
            llm = _init_llm()
            collector = NewsCollector(llm_service=llm)
            collector.collect(date)
            items = collector.get_news(date, limit=8)
        for item in items:
            item["sentiment"] = _classify_news(item.get("title", ""), item.get("summary", ""))
        return items
    except Exception as e:
        logger.warning(f"News failed: {e}")
        return []


def format_news(items):
    if not items: return "暂无重要新闻"
    lines = []
    for i, item in enumerate(items[:5], 1):
        title = item.get("title", "")
        raw_summary = item.get("summary", "").strip()
        summary = _clean_summary(raw_summary, title)
        tag = {"bullish": "[利好]", "bearish": "[利空]", "neutral": ""}.get(item.get("sentiment", "neutral"), "")
        display = summary[:100] + "..." if len(summary) > 100 else summary
        lines.append(f"{i}. {title}  {tag}")
        if display:
            lines.append(f"   {display}")
    return "\n".join(lines)


# ============================================================
# 3. 大盘指数
# ============================================================
def build_indices():
    import requests
    try:
        resp = requests.get("http://qt.gtimg.cn/q=sh000001,sz399001,sz399006,sh000688,sh000300", timeout=10)
        resp.encoding = "gbk"
        indices = []
        for line in resp.text.strip().split("\n"):
            if not line.strip(): continue
            parts = line.split("~")
            if len(parts) < 34: continue
            try:
                indices.append({"name": parts[1], "price": float(parts[3]), "pct": round(float(parts[32]), 2)})
            except (ValueError, IndexError): pass
        return indices
    except Exception as e:
        logger.warning(f"Indices failed: {e}")
        return []


def format_indices(indices):
    if not indices: return "数据暂不可用", None
    lines = []
    for idx in indices:
        pct = idx["pct"]
        color = "red" if pct > 0 else "green"; arrow = "↑" if pct > 0 else "↓"
        lines.append(f"{idx['name']}: {idx['price']:,.2f} <font color='{color}'>{arrow}{abs(pct):.2f}%</font>")
    body = "\n".join(lines)

    columns = []
    for idx in indices:
        pct = idx["pct"]
        color = "red" if pct > 0 else "green"
        arrow = "↑" if pct > 0 else "↓"
        columns.append({
            "width": 1,
            "items": [
                {"text": idx["name"], "size": "normal"},
                {"text": f"{idx['price']:,.2f}", "size": "normal"},
                {"text": f"{color} {arrow}{abs(pct):.2f}%", "color": color, "size": "normal"},
            ]
        })
    return body, columns


# ============================================================
# 4. 持仓分析
# ============================================================
def build_positions(dry_run: bool = False):
    db = None
    try:
        db = _preview_execution_db() if dry_run else Database()
        config = Config(); config.load()
        data_layer = sl = None
        if not dry_run:
            data_layer = DataLayer(config=config, db=db); data_layer._db = db
            sl = StrategyLayer(config=config, data_layer=data_layer, db=db)
        el = ExecutionLayer(db=db, config=config, data_layer=data_layer, strategy_layer=sl)
        el.start()
        # dry-run 零写副作用: 只读快照, 不触发退出后检查/台账写 (W1 任务D)。
        if not dry_run:
            try: el.daily_update()
            except: pass
        snapshot = el.get_portfolio_snapshot()
        rows = el.filter_trades(status="pending_entry", limit=15)
        pending = []
        for r in rows:
            code = r.code if hasattr(r, 'code') else r.get("code", "")
            info = data_layer.get_stock_info(code) if data_layer else None
            name = (info.name if hasattr(info, 'name') else "") or (r.name if hasattr(r, 'name') else "") or code
            industry = info.industry if hasattr(info, 'industry') else ""
            score = (r.current_score if hasattr(r, 'current_score') else None) or (r.initial_score if hasattr(r, 'initial_score') else None) or 0
            reason = (r.watch_reason if hasattr(r, 'watch_reason') else None) or (r.entry_reason if hasattr(r, 'entry_reason') else None) or ""
            pending.append({"code": code, "name": name, "industry": industry,
                            "score": score, "reason": reason})
        return {"snapshot": snapshot, "pending": pending}
    except Exception as e:
        logger.warning(f"Positions failed: {e}")
        return None
    finally:
        if dry_run and db is not None:
            db.close()


def format_positions(data):
    if not data: return "持仓数据暂不可用"
    snapshot = data["snapshot"]
    pending = data["pending"]
    nav = getattr(snapshot, "portfolio_nav", getattr(snapshot, "total_nav", 0)) or 0
    lines = [f"组合净值: {nav:,.2f}"]
    if pending:
        lines.append(f"\n🟡 待入场 ({len(pending)} 只)")
        for p in pending[:10]:
            ind = f" — {p['industry']}" if p['industry'] else ""
            lines.append(f"  {p['code']} {p['name']}{ind} | 评分 {p['score']:.2f}")
    else:
        lines.append("\n🟡 待入场: 0 只")
    lines.append("\n📌 今日盯盘\n  当前无活跃持仓，暂无盯盘要点")
    return "\n".join(lines)


# ============================================================
# 5. 今日选股
# ============================================================

def _get_stock_data(dry_run: bool = False):
    import json
    conn = None
    try:
        conn = _readonly_connection() if dry_run else Database().get_connection()
        # 最新因子日
        latest = conn.execute("SELECT MAX(date) as d FROM factor_daily WHERE factor_scores IS NOT NULL").fetchone()
        if not latest or not latest["d"]: return []
        # 用策略层权威输出 model_predictions.blended_score 排序(已含 composite*0.8+ml*0.2)
        rows = conn.execute("""
            SELECT m.code, s.name, s.industry, m.blended_score,
                   COALESCE(m.ml_score, 0.0) AS ml_score,
                   f.factor_scores
            FROM model_predictions m
            JOIN factor_daily f ON m.code = f.code AND m.date = f.date
            LEFT JOIN stock_info s ON m.code = s.code
            WHERE m.date = ? AND m.blended_score IS NOT NULL
        """, (latest["d"],)).fetchall()
        scored = []
        for row in rows:
            d = dict(row)
            scores = json.loads(d["factor_scores"]) if d["factor_scores"] else {}
            d["composite"] = float(d.get("blended_score") or 0.0)
            d["name"] = d["name"] or d["code"]; d["industry"] = d["industry"] or "未知"
            scored.append((d, scores))
        scored.sort(key=lambda x: x[0]["composite"], reverse=True)
        return scored[:5]
    except Exception as e:
        logger.warning(f"Stock data failed: {e}")
        return []
    finally:
        if dry_run and conn is not None:
            conn.close()


# IC 负向因子集合 — 与 aggregator.py IC_NEGATIVE_FACTORS 同源 (对拍保证一致)。
# 聚合器在 composite 前对这些因子取负 (raw -0.63 → adjusted +0.63 计为正贡献)；
# 理由段若不翻号, 会把「实际加分的负向因子」说成「偏弱」—— 2026-11-12 实测
# Top5 全被"cntd10偏弱"类文案占据, 方向说反 (第二三二环待修项②)。
try:
    from strategy_layer.aggregation.aggregator import IC_NEGATIVE_FACTORS as _IC_NEG
except Exception:  # 防御: 理由段降级不影响主流程, 回退空集 (退回旧行为)
    _IC_NEG = frozenset()

# Qlib 因子名中文化 (第二三二环: cn_map 未覆盖现代 89 键, 显示英文缩写)
QLIB_CN_MAP = {
    "alpha_001": "价量因子α1", "alpha_002": "价量因子α2", "alpha_003": "价量因子α3",
    "alpha_004": "量价冲击α4", "alpha_005": "动量α5", "alpha_006": "反转α6",
    "alpha_007": "量价背离α7", "alpha_008": "量价共振α8", "alpha_009": "动量α9",
    "alpha_010": "反转α10", "alpha_011": "量价因子α11", "alpha_012": "波动α12",
    "alpha_013": "量价因子α13", "alpha_014": "动量α14", "alpha_015": "量价因子α15",
    "alpha_016": "波动α16", "alpha_017": "量价因子α17", "alpha_018": "动量α18",
    "alpha_019": "量价因子α19", "alpha_020": "反转α20", "alpha_48": "量价因子α48",
    "bb_width": "布林带宽", "cntd5": "5日连跌数", "cntd10": "10日连跌数",
    "cntd20": "20日连跌数", "cntp5": "5日连涨数", "cntp10": "10日连涨数",
    "cntp20": "20日连涨数", "concept_count": "概念数", "concept_heat": "概念热度",
    "cord5": "5日价量相关", "cord20": "20日价量相关", "corr5": "5日量价相关",
    "corr10": "10日量价相关", "klen": "K线实体", "klow": "下影线", "klow2": "下影线2",
    "kmid": "实体占比", "kmid2": "实体占比2", "ksft": "上影偏移", "ksft2": "上影偏移2",
    "kup": "上影线", "kup2": "上影线2", "liquidity_illiquid": "非流动性",
    "lhb_net_buy": "龙虎榜净买", "ma5_dev": "5日均线乖离", "ma10_dev": "10日均线乖离",
    "macd_hist": "MACD柱", "market_emotion_proxy": "市场情绪代理",
    "market_sentiment": "市场情绪", "max5": "5日最高", "min5": "5日最低",
    "momentum_5d_reversal": "5日反转", "moneyflow_main_net": "主力净流入",
    "overnight_gap": "隔夜跳空", "pattern_technical": "技术形态",
    "rank5": "5日分位", "rank10": "10日分位", "roc5": "5日变动率", "roc10": "10日变动率",
    "roc20": "20日变动率", "roc60": "60日变动率", "rsi14": "RSI(14)",
    "seasonality_same_month": "同月季节性", "std5": "5日波动", "std10": "10日波动",
    "std20": "20日波动", "sumd5": "5日跌量", "sumd10": "10日跌量", "sumd20": "20日跌量",
    "sump5": "5日涨量", "sump10": "10日涨量", "sump20": "20日涨量",
    "turnover_ratio": "换手率", "vma5": "5日均量", "vma10": "10日均量", "vma20": "20日均量",
    "vstd5": "5日量波动", "vstd10": "10日量波动", "analyst_consensus": "分析师共识",
    "industry_momentum": "行业动量", "leverage_low": "低杠杆",
}
CN_MAP = {
    "momentum_20d": "20日涨幅", "value_discount": "估值", "roe_quality": "ROE",
    "growth_combined": "成长性", "moneyflow_net": "资金流入", "rsi_14": "RSI",
    "avg_turnover_rate": "换手率", "market_sentiment": "市场情绪",
    "earnings_stability": "盈利稳定", "asset_turnover": "资产周转",
    "dividend_yield": "股息", "size_small_cap": "小市值", "volatility_reversal": "波动率",
    **QLIB_CN_MAP,
}


def _template_stock_reason(d, scores):
    """选股理由 = 按「权重 × 方向调整后分数」取真实贡献前 3 (与 composite 同源)。

    口径: adjusted = raw if 因子不在 IC_NEGATIVE_FACTORS else -raw;
          贡献 = weights[factor] × adjusted。只显示贡献 >0 的因子为「偏强」,
          <0 为「偏弱」—— 与聚合器对 composite 的实际计算一致 (第二三二环修)。
    """
    import sqlite3 as _sq
    from pathlib import Path as _P
    from common.database import Database as _DB
    name = d["name"]; code = d["code"]; industry = d["industry"]; composite = d["composite"]
    # 权重: 与策略层同源 (factor_weights 最新 weight_date); 取不到时回退等权
    weights = None
    try:
        _c = _sq.connect(_P(_DB().db_path).resolve().as_uri() + "?mode=ro", uri=True)
        _r = _c.execute("SELECT MAX(weight_date) FROM factor_weights").fetchone()
        _wd = _r[0] if _r else None
        if _wd:
            weights = {k: v for k, v in _c.execute(
                "SELECT factor_name, weight FROM factor_weights WHERE weight_date = ?", (_wd,))}
        _c.close()
    except Exception:
        weights = None
    adjusted = {k: (-v if k in _IC_NEG else v) for k, v in scores.items()}
    if weights:
        contrib = [(k, weights[k] * adjusted[k]) for k in adjusted
                   if k in weights and abs(adjusted[k]) > 0.05]
    else:  # 权重不可得: 退化为按调整后分数绝对值 (至少方向是对的)
        contrib = [(k, adjusted[k]) for k in adjusted if abs(adjusted[k]) > 0.05]
    contrib.sort(key=lambda x: abs(x[1]), reverse=True)
    reasons = []
    for fname, cscore in contrib[:3]:
        cn = CN_MAP.get(fname, fname)
        if cscore > 0.05: reasons.append(f"{cn}贡献正({cscore:+.3f})")
        elif cscore < -0.05: reasons.append(f"{cn}拖累({cscore:+.3f})")
    reasons_str = "；".join(reasons) if reasons else "综合评分较高"
    return f"{name}({code}) — {industry}\n  综合评分: {composite:.3f}\n  因子: {reasons_str}"


def build_stock_picks(dry_run: bool = False):
    raw = _get_stock_data(dry_run=dry_run)
    if not raw: return []
    results = []
    llm = None if dry_run else _init_llm()
    llm_results = {}
    if llm and llm.is_available():
        items = []
        for d, scores in raw:
            # 给 LLM 的因子文案与 composite 同源: 负向因子先翻号, 按调整后分数取前 5
            adjusted = {k: (-v if k in _IC_NEG else v) for k, v in scores.items()}
            top = sorted(
                [(k, v) for k, v in adjusted.items() if abs(v) > 0.05],
                key=lambda x: abs(x[1]), reverse=True,
            )[:5]
            factor_text = "；".join(f"{CN_MAP.get(k, k)}={v:+.2f}" for k, v in top)
            items.append(f"{d['name']}({d['code']}) 行业{d['industry']} 评分{d['composite']:.3f} 因子(方向已调整):{factor_text}")
        prompt = (
            f"分析以下5只A股，对每只给出1-2句话分析理由+技术面判断+操作建议，用序号1-5开头:\n\n"
            + "\n".join(items)
        )
        r = _llm_call(prompt, timeout_s=30, max_tokens=2000, point="stock_reason_batch")
        if r:
            for line in r.strip().split("\n"):
                line = line.strip()
                if not line: continue
                m = re.match(r'^(\d+)[.、]\s*', line)
                if m:
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < len(raw):
                        text = re.sub(r'^(\d+)[.、]\s*\*{0,2}', '', line).strip()
                        llm_results[idx] = text
    for i, (d, scores) in enumerate(raw):
        if i in llm_results:
            text = re.sub(r'\*{1,2}', '', llm_results[i]).strip()
            narrative = f"{d['name']}({d['code']}) — {d['industry']}\n  综合评分: {d['composite']:.3f}\n  {text}"
        else:
            narrative = _template_stock_reason(d, scores)
        results.append({"code": d["code"], "name": d["name"], "industry": d["industry"],
                        "composite": d["composite"], "narrative": narrative})
    return results


def format_stock_picks(stocks):
    if not stocks: return "暂无推荐"
    return "\n".join(f"{i}. {s['narrative']}" for i, s in enumerate(stocks[:5], 1))


# ============================================================
# 6. 大盘研判
# ============================================================
def build_market_outlook(indices, dry_run: bool = False):
    if not indices: return None
    index_text = "; ".join(f"{i['name']}: {i['price']} ({i['pct']:+.2f}%)" for i in indices[:3])
    prompt = (
        f"今日A股指数数据:\n{index_text}\n\n"
        f"请分析今日A股市场，给出以下格式，不要markdown:\n"
        f"【结论】一句话总结今日方向\n"
        f"【分析】2-3句关键分析\n"
        f"【操作】仓位和操作建议"
    )
    r = None if dry_run else _llm_call(prompt, timeout_s=20, max_tokens=2000, point="market_outlook")
    if r:
        return re.sub(r'#{1,6}\s*\*?', '', r.strip())
    up = sum(1 for i in indices if i["pct"] > 0)
    direction = "偏多" if up > len(indices) / 2 else "偏空" if up < len(indices) / 2 else "震荡"
    return f"【结论】{direction}\n【分析】指数分化\n【操作】5-7成仓位"


def format_market_outlook(body):
    return body or "研判暂不可用"


# ============================================================
# 构建
# ============================================================
def build_morning_brief(date, dry_run: bool = False):
    sections = []

    overnight = build_overnight()
    sections.append({"title": "🌙 昨夜外盘", "body": format_overnight(overnight),
                     "subtitle": "新浪财经实时行情 · 美股/原油/黄金/A50"})

    news = build_news(date, dry_run=dry_run)
    sections.append({"title": "📰 今日要闻", "body": format_news(news),
                     "subtitle": "东方财富新闻源 · 关键词分类利好/利空"})

    indices = build_indices()
    idx_body, idx_columns = format_indices(indices)
    section = {"title": "📊 大盘指数", "body": idx_body,
               "subtitle": "腾讯财经实时行情接口"}
    if idx_columns:
        section["columns"] = idx_columns
    sections.append(section)

    pos_data = build_positions(dry_run=dry_run)
    sections.append({"title": "📋 持仓分析", "body": format_positions(pos_data),
                     "subtitle": "执行层 StockPool · 策略层推荐候选"})

    stocks = build_stock_picks(dry_run=dry_run)
    sections.append({"title": "📌 今日选股", "body": format_stock_picks(stocks),
                     "subtitle": "策略层因子评分系统 · 综合动量/价值/质量排序 Top 5"})

    outlook = build_market_outlook(indices, dry_run=dry_run)
    sections.append({"title": "🤖 大盘研判", "body": format_market_outlook(outlook),
                     "subtitle": "LLM 基于指数数据和市场情绪的综合分析"})

    now = bj_now()[:19]
    sections.append({"title": "", "body": f"<font color='grey'>QuantStar 量化投研系统 | {now} | 仅供参考</font>"})

    return {"sections": sections, "date": date}


def main():
    dry_run = "--dry-run" in sys.argv
    logger.info(f"构建早报: {DATE} (dry_run={dry_run})")
    content = build_morning_brief(DATE, dry_run=dry_run)

    print(f"\n{'='*50}")
    print(f"🌅 QuantStar 早报 — {DATE}")
    print(f"{'='*50}")
    for s in content["sections"]:
        t = s.get('title', '')
        sub = s.get('subtitle', '')
        b = s.get('body', '')
        print(f"\n{t}")
        if sub: print(f"  {sub}")
        print(b)
    print(f"\n{'='*50}")

    output_dir = os.path.join("output", "preview")
    os.makedirs(output_dir, exist_ok=True)
    save_path = os.path.join(output_dir, f"morning_brief_{DATE}.txt")
    with open(save_path, "w", encoding="utf-8") as f:
        for s in content["sections"]:
            f.write(f"--- {s['title']} ---\n")
            if s.get('subtitle'): f.write(f"  {s['subtitle']}\n")
            f.write(f"{s['body']}\n\n")
    logger.info(f"预览已保存: {save_path}")

    if dry_run:
        print(f"\n⏸️  dry-run 模式，未推送。查看预览: {save_path}")
        return

    config = Config(); config.load()
    msg = Message(content=content, content_type="morning_brief", title=f"🌅 QuantStar 早报 — {DATE}")
    msg.content = FeishuCardFormatter().format(msg)
    dl = DeliveryLayer(config=config)
    dl.registry.mark_healthy("feishu_dm")
    dl.registry.mark_healthy("feishu_group")
    # send() 只调一次：三元判断里重复求值会把首条结果丢掉（重复推送病根）。
    results = dl.send(msg)
    if not isinstance(results, list):
        results = [results]
    for r in results:
        print(f"推送 [{getattr(r,'channel','?')}]: {getattr(r,'status','?')}")
    print(f"已推送")


if __name__ == "__main__":
    main()

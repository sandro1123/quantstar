#!/usr/bin/env python3
"""
晚报 v3 — 盘面总览 + 个股精选
对标旧系统 generate_evening_report.py 质量。
Card1: 盘面总览 | Card2: 个股精选
"""
import sys, os, json, re, concurrent.futures, sqlite3
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
from execution_layer import ExecutionLayer
from agent_layer.llm.service import LLMService
from agent_layer.llm.provider_manager import ProviderManager

logger = get_logger("scripts.evening_report")
DATE = bj_date()
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_llm_cache = None


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


def _llm_call(prompt, timeout_s=8, max_tokens=2000, point="llm"):
    llm = _init_llm()
    if not llm or not llm.is_available():
        return None
    def _do():
        return llm.call(prompt, temperature=0.3, max_tokens=max_tokens, call_point=point)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_do)
            return fut.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        return None
    except Exception:
        return None


def _db():
    return Database().get_connection()


def _display_date(date_str):
    try:
        from datetime import datetime
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{dt.month}月{dt.day}日 {WEEKDAYS[dt.weekday()]}"
    except Exception:
        return date_str


def _fmt_pct(v, plus=True):
    if v is None: return "--"
    s = f"{v:+.2f}%" if plus else f"{v:.2f}%"
    return s


def _fmt_mv(mv):
    if not mv: return ""
    y = mv / 1e8
    if y >= 100: return f"{y:.0f}亿"
    if y >= 1: return f"{y:.1f}亿"
    return f"{mv/1e4:.0f}万"


# ============================================================
# 数据获取
# ============================================================

def fetch_indices():
    """获取指数行情: qt.gtimg.cn 优先（同晨报），FuyaoClient 回退"""
    import requests as _req
    try:
        resp = _req.get("http://qt.gtimg.cn/q=sh000001,sz399001,sz399006,sh000688,sh000300", timeout=10)
        resp.encoding = "gbk"
        result = []
        for line in resp.text.strip().split("\n"):
            if not line.strip(): continue
            parts = line.split("~")
            if len(parts) < 34: continue
            try:
                result.append({"name": parts[1], "price": float(parts[3]),
                               "change_pct": round(float(parts[32]), 2)})
            except (ValueError, IndexError): pass
        if result:
            return result
    except Exception as e:
        logger.debug(f"qt.gtimg.cn index failed: {e}")
    try:
        from data_layer.sources.fuyao_client import FuyaoClient
        fc = FuyaoClient()
        if fc.available():
            codes = "000001.SH,399001.SZ,399006.SZ,000688.SH,000300.SH"
            snap = fc.get_index_snapshot(codes)
            if snap:
                name_map = {"上证指数": "上证", "深证成指": "深证", "创业板指": "创业板",
                            "科创50": "科创50", "沪深300": "沪深300"}
                return [{"name": name_map.get(it.get("name",""), it.get("name","")),
                         "price": it.get("price", 0), "change_pct": it.get("change_pct", 0)}
                        for it in snap]
    except Exception as e:
        logger.debug(f"FuyaoClient index failed: {e}")
    return []


def fetch_latest_dates():
    """获取数据库中最新日期"""
    conn = _db()
    try:
        kline_date = conn.execute("SELECT MAX(date) FROM daily_klines").fetchone()[0]
        factor_date = conn.execute("SELECT MAX(date) FROM factor_daily WHERE change_pct IS NOT NULL").fetchone()[0]
        sentiment_date = conn.execute("SELECT MAX(date) FROM market_sentiment").fetchone()[0]
        return {
            "kline": kline_date or DATE,
            "factor": factor_date or DATE,
            "sentiment": sentiment_date or DATE,
        }
    finally:
        conn.close()


def fetch_market_snapshot(kline_date=None):
    """涨跌分布"""
    conn = _db()
    try:
        query_date = kline_date or DATE
        row = conn.execute("""
            SELECT
                SUM(CASE WHEN change_pct>0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct<0 THEN 1 ELSE 0 END),
                AVG(change_pct),
                SUM(CASE WHEN change_pct>=9.8 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct<=-9.8 THEN 1 ELSE 0 END),
                COUNT(*)
            FROM daily_klines WHERE date=?
        """, (query_date,)).fetchone()
        if not row:
            return None
        total = row[5] or 0
        if total == 0:
            return None
        up, dn, avg, zt, dt, total = row
        return {"up": up or 0, "dn": dn or 0, "flat": total - (up or 0) - (dn or 0),
                "avg": round(avg or 0, 2), "zt": zt or 0, "dt": dt or 0,
                "ratio": round(up / max(dn, 1), 2), "total": total, "date": query_date}
    finally:
        conn.close()


def fetch_industry_perf(kline_date=None):
    """行业板块涨跌"""
    conn = _db()
    try:
        query_date = kline_date or DATE
        rows = conn.execute("""
            SELECT si.industry, COUNT(*), ROUND(AVG(dk.change_pct),2),
                   SUM(CASE WHEN dk.change_pct>=9.8 THEN 1 ELSE 0 END)
            FROM daily_klines dk JOIN stock_info si ON dk.code=si.code
            WHERE dk.date=? AND si.industry IS NOT NULL AND si.industry!=''
            GROUP BY si.industry HAVING COUNT(*)>=5
            ORDER BY AVG(dk.change_pct) DESC
        """, (query_date,)).fetchall()
        return rows
    finally:
        conn.close()


def fetch_sentiment():
    """市场情绪"""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM market_sentiment WHERE date=? ORDER BY date DESC LIMIT 1", (DATE,)
        ).fetchone()
        if row:
            return {"market_temp": row[1], "limit_up": row[2], "limit_down": row[3],
                    "up": row[4], "down": row[5], "zt": row[6], "dt": row[7]}
    except Exception:
        pass
    finally:
        conn.close()
    return None


def fetch_lhb_data():
    """龙虎榜 TOP5 净买入/净卖出（名称回退到 stock_info）"""
    conn = _db()
    try:
        max_date = conn.execute("SELECT MAX(date) FROM lhb_daily").fetchone()[0]
        if not max_date:
            return None
        top_buy = conn.execute("""
            SELECT l.code, si.name, l.net_buy_amount, l.total_buy, l.total_sell, dk.change_pct
            FROM lhb_daily l
            LEFT JOIN stock_info si ON l.code=si.code
            LEFT JOIN daily_klines dk ON l.code=dk.code AND dk.date=?
            WHERE l.date=? AND l.net_buy_amount IS NOT NULL
            ORDER BY l.net_buy_amount DESC LIMIT 5
        """, (DATE, max_date)).fetchall()
        top_sell = conn.execute("""
            SELECT l.code, si.name, l.net_buy_amount, l.total_buy, l.total_sell, dk.change_pct
            FROM lhb_daily l
            LEFT JOIN stock_info si ON l.code=si.code
            LEFT JOIN daily_klines dk ON l.code=dk.code AND dk.date=?
            WHERE l.date=? AND l.net_buy_amount IS NOT NULL
            ORDER BY l.net_buy_amount ASC LIMIT 5
        """, (DATE, max_date)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM lhb_daily WHERE date=?", (max_date,)).fetchone()[0]
        net_buy_count = conn.execute(
            "SELECT COUNT(*) FROM lhb_daily WHERE date=? AND net_buy_amount>0", (max_date,)
        ).fetchone()[0]
        return {"top_buy": top_buy, "top_sell": top_sell, "total": total,
                "net_buy_count": net_buy_count, "date": max_date}
    finally:
        conn.close()


def fetch_positions():
    """当前持仓（名称回退到 stock_info）"""
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT sp.code, sp.name, sp.entry_price, sp.last_price, sp.total_return_pct,
                   sp.holding_days, sp.stop_loss, sp.current_score, sp.status,
                   si.industry, si.name
            FROM stock_pool sp
            LEFT JOIN stock_info si ON sp.code=si.code
            WHERE sp.status IN ('active', 'pending_entry')
            ORDER BY sp.total_return_pct DESC
        """).fetchall()
        return [{"code": r[0], "name": r[1] or (r[10] or ""), "entry": r[2], "last": r[3],
                 "pnl_pct": r[4] or 0, "days": r[5] or 0, "stop": r[6] or 0,
                 "score": r[7] or 0, "status": r[8], "industry": r[9] or ""}
                for r in rows]
    finally:
        conn.close()


def fetch_stock_detail(code):
    """个股详情: factor_daily + stock_financials + daily_klines"""
    conn = _db()
    try:
        max_fd = conn.execute("SELECT MAX(date) FROM factor_daily WHERE code=?", (code,)).fetchone()[0]
        fd = None
        if max_fd:
            fd = conn.execute("""
                SELECT pe_ttm, pb, roe, pe_rank, pb_rank, roe_rank, momentum_rank, macd_rank,
                       momentum_5d, momentum_20d, rsi_14, rsi6, ma_status, macd_hist,
                       volume_ratio, turnover_rate, total_mv, change_pct, factor_scores
                FROM factor_daily WHERE code=? AND date=?
            """, (code, max_fd)).fetchone()

        fin = conn.execute("""
            SELECT gross_margin, net_margin, total_liabilities, revenue, profit
            FROM stock_financials WHERE code=? ORDER BY date DESC LIMIT 1
        """, (code,)).fetchone()

        klines = conn.execute("""
            SELECT date, close, low, high, volume, change_pct
            FROM daily_klines WHERE code=? ORDER BY date DESC LIMIT 60
        """, (code,)).fetchall()

        info = conn.execute(
            "SELECT name, industry FROM stock_info WHERE code=?", (code,)
        ).fetchone()

        val = conn.execute(
            "SELECT pe_ttm, pb_mrq FROM stock_valuation WHERE code=? AND date=?",
            (code, DATE)
        ).fetchone()

        return {"factor": fd, "financial": fin, "klines": klines,
                "name": info[0] if info else "", "industry": info[1] if info else "",
                "pe": val[0] if val else None, "pb": val[1] if val else None}
    finally:
        conn.close()


def fetch_yesterday_review():
    """昨日推荐回顾"""
    conn = _db()
    try:
        prev = conn.execute(
            "SELECT DISTINCT DATE(created_at) as d FROM action_records WHERE DATE(created_at) < DATE(?) ORDER BY d DESC LIMIT 1", (DATE,)
        ).fetchone()
        if not prev:
            return None
        prev_date = prev[0]
        actions = conn.execute("""
            SELECT ar.code, si.name, ar.action_type, ar.price, ar.reason
            FROM action_records ar
            LEFT JOIN stock_info si ON ar.code=si.code
            WHERE DATE(ar.created_at) = ? AND ar.action_type IN ('entry', 'status_change')
            ORDER BY ar.created_at DESC
        """, (prev_date,)).fetchall()
        if not actions:
            return None
        results = []
        seen = set()
        for code, name, atype, price, reason in actions:
            if code in seen: continue
            seen.add(code)
            if atype != 'entry': continue
            kline = conn.execute(
                "SELECT change_pct FROM daily_klines WHERE code=? AND date=?", (code, DATE)
            ).fetchone()
            chg = kline[0] if kline else None
            results.append({"code": code, "name": name or code, "price": price, "chg": chg})
        wins = sum(1 for r in results if r["chg"] and r["chg"] > 0)
        avg_chg = sum(r["chg"] or 0 for r in results) / max(len(results), 1)
        return {"date": prev_date, "stocks": results, "wins": wins, "total": len(results),
                "avg_chg": round(avg_chg, 2)}
    finally:
        conn.close()


def fetch_watch_list():
    """关注/待入场列表（名称回退到 stock_info）"""
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT sp.code, sp.name, sp.current_score, sp.added_reason, sp.watch_reason,
                   si.industry, si.name
            FROM stock_pool sp
            LEFT JOIN stock_info si ON sp.code=si.code
            WHERE sp.status IN ('watch', 'pending_entry')
            ORDER BY sp.current_score DESC LIMIT 10
        """).fetchall()
        return [{"code": r[0], "name": r[1] or (r[6] or ""), "score": r[2] or 0,
                 "reason": r[3] or r[4] or "", "industry": r[5] or ""}
                for r in rows]
    finally:
        conn.close()


def fetch_today_actions():
    """今日操作（按 code+type 去重，只保留最新一条）"""
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT ar.code, si.name, ar.action_type, ar.price, ar.reason, ar.created_at
            FROM action_records ar
            LEFT JOIN stock_info si ON ar.code=si.code
            WHERE ar.created_at >= ? AND ar.created_at < ?
            ORDER BY ar.created_at DESC
        """, (DATE, DATE + chr(32) + 'z')).fetchall()
        seen = set()
        deduped = []
        for code, name, atype, price, reason, _ in rows:
            key = f"{code}:{atype}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append({"code": code, "name": name or code, "type": atype,
                           "price": price, "reason": reason or ""})
        return deduped
    finally:
        conn.close()


# ============================================================
# 格式化
# ============================================================

def fmt_indices(indices):
    """格式化指数行情"""
    if not indices:
        return "指数数据暂不可用"
    parts = []
    for idx in indices:
        pct = idx["change_pct"]
        icon = "🔴" if pct >= 0 else "🟢"
        parts.append(f"{icon}{idx['name']} {idx['price']:.2f}（{pct:+.2f}%）")
    return " | ".join(parts)


def fmt_market_judgment(bs, inds):
    """核心判断"""
    if not bs:
        return "数据加载中"
    up_r = bs["up"] / max(bs["total"], 1) * 100
    if up_r > 60:
        mood = "市场整体偏强" if bs["zt"] < 50 else "赚钱效应显著"
        pos = "6-7成"
    elif up_r > 50:
        mood = "震荡偏强"
        pos = "5-6成"
    elif up_r > 40:
        mood = "震荡偏弱"
        pos = "4-5成"
    else:
        mood = "市场承压" if bs["dt"] < 20 else "亏钱效应明显"
        pos = "3-4成"
    top = f"最强：{inds[0][0]}（{inds[0][2]:+.2f}%）" if inds else ""
    return (
        f"{mood}，建议仓位 {pos}\n"
        f"上涨{bs['up']}/{bs['total']}（{up_r:.0f}%）| 下跌{bs['dn']}/{bs['total']}（{100-up_r:.0f}%）| "
        f"涨停{bs['zt']}|跌停{bs['dt']}|均值{bs['avg']:+.2f}%\n"
        f"{top}"
    )


def fmt_industry(inds):
    """板块轮动"""
    if not inds:
        return "暂无行业数据"
    up = [i for i in inds if i[2] > 0]
    dn = sorted([i for i in inds if i[2] < 0], key=lambda x: x[2])
    flat = [i for i in inds if i[2] == 0]
    if not up and not dn and flat:
        return f"共 {len(flat)} 个行业，但涨跌幅数据暂未填充（全部为 0%）"
    lines = []
    if up:
        lines.append(f"强势板块（共{len(up)}个上涨）：")
        for i in up[:5]:
            lines.append(f"  🔴 {i[0]} {i[2]:+.2f}%（涨停{i[3]}只）")
    if dn:
        lines.append(f"\n弱势板块（共{len(dn)}个下跌）：")
        for i in dn[:5]:
            lines.append(f"  🟢 {i[0]} {i[2]:+.2f}%")
    if flat:
        lines.append(f"\n⚪ 持平（{len(flat)}个行业涨幅为0%）")
    return "\n".join(lines)


def fmt_risk(bs, inds):
    """风险监控"""
    lines = []
    dn_r = bs["dn"] / max(bs["total"], 1) * 100
    if dn_r > 60:
        lines.append(f"⚠️ 下跌占比过高（{dn_r:.0f}%），注意控制仓位")
    if bs["dt"] > 10:
        lines.append(f"⚠️ 跌停{bs['dt']}只，局部恐慌")
    if bs["zt"] > 50:
        lines.append(f"✅ 涨停{bs['zt']}只，短线情绪活跃")
    if inds:
        spread = inds[0][2] - sorted(inds, key=lambda x: x[2])[0][2]
        lines.append(f"板块分化 {'大' if spread > 5 else '中' if spread > 3 else '小'}（TOP vs BOTTOM差{abs(spread):.1f}%）")
    return "\n".join(lines) if lines else "系统运行正常"


def fmt_tomorrow(inds):
    """明日展望"""
    if not inds:
        return "数据暂缺"
    top3 = "、".join(i[0] for i in inds[:3])
    bot3 = "、".join(i[0] for i in sorted(inds, key=lambda x: x[2])[:3])
    return (
        f"🔴 关注延续：{top3}\n🟢 回避弱势：{bot3}\n"
        f"{'大盘普涨后注意分化' if inds[0][2] > 2 else '控制仓位等待方向'}"
    )


def fmt_lhb(lhb):
    """龙虎榜"""
    if not lhb:
        return None
    lines = [f"上榜{lhb['total']}次 | 净买入{lhb['net_buy_count']}次"]
    if lhb["top_buy"]:
        lines.append("\n🔥 净买入 TOP5")
        for i, (code, name, net, buy, sell, chg) in enumerate(lhb["top_buy"], 1):
            net_yi = abs(net) / 1e8 if net else 0
            chg_str = f"{chg:+.2f}%" if chg else "--"
            lines.append(f"  {i}. {name or code}({code}) {chg_str} 净买入{net_yi:.1f}亿")
    if lhb["top_sell"]:
        lines.append("\n📉 净卖出 TOP5")
        for i, (code, name, net, buy, sell, chg) in enumerate(lhb["top_sell"], 1):
            net_yi = abs(net) / 1e8 if net else 0
            chg_str = f"{chg:+.2f}%" if chg else "--"
            lines.append(f"  {i}. {name or code}({code}) {chg_str} 净卖出{net_yi:.1f}亿")
    return "\n".join(lines)


# ============================================================
# 个股分析
# ============================================================

def calc_percentile(values, current):
    if not values or current is None: return None
    below = sum(1 for v in values if v < current)
    return round(below / len(values) * 100)


def sr_levels(klines):
    if not klines or len(klines) < 5:
        return None, None
    lows = [k[2] for k in klines[:20] if k[2]]
    highs = [k[3] for k in klines[:20] if k[3]]
    return min(lows) if lows else None, max(highs) if highs else None


def rank_desc(label, rank_val, high_is_good=True):
    if rank_val is None: return ""
    if not high_is_good:
        if rank_val < 20: return f"{label}处于极低水平（前{rank_val:.0f}%），非常便宜"
        if rank_val < 40: return f"{label}偏低（前{rank_val:.0f}%），估值有优势"
        if rank_val < 60: return f"{label}处于中位（第{rank_val:.0f}百分位），估值适中"
        if rank_val < 80: return f"{label}偏高（第{rank_val:.0f}百分位），需关注"
        return f"{label}处于高位（第{rank_val:.0f}百分位），估值偏高"
    else:
        if rank_val > 80: return f"{label}表现强劲（前{100-rank_val:.0f}%），处于领先位置"
        if rank_val > 60: return f"{label}偏强（第{rank_val:.0f}百分位），表现中上"
        if rank_val > 40: return f"{label}中位（第{rank_val:.0f}百分位），表现一般"
        if rank_val > 20: return f"{label}偏弱（第{rank_val:.0f}百分位），表现欠佳"
        return f"{label}疲弱（前{100-rank_val:.0f}%），处于落后位置"


def _parse_fs(fd):
    """解析 factor_scores JSON，返回 dict"""
    if not fd:
        return {}
    try:
        raw = fd[18] if len(fd) > 18 and fd[18] else "{}"
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {}


def _z_to_pct(z):
    """z-score 转近似百分位 rank（0-100）"""
    if z is None: return None
    return round(min(99, max(1, (z + 2.5) / 5 * 100)))


def classify_stock_type(fd, fin, score=0):
    """股票类型分类（支持 factor_scores 回退）"""
    if not fd:
        return "📊", "综合评分", f"评分{score}，因子数据有限"
    pe_r, pb_r, roe_r = fd[3], fd[4], fd[5]
    mom_r, macd_r, mv = fd[6], fd[7], fd[16] or 0
    if any(v is not None for v in [pe_r, pb_r, roe_r, mom_r, macd_r]):
        is_large = mv >= 1e10
        if all(r is not None and r < 30 for r in [pe_r, pb_r]) and roe_r and roe_r > 60 and is_large:
            return "🏆", "深度价值", f"PE/ROE双优，安全边际充足"
        if mom_r and mom_r > 70 and macd_r and macd_r > 60:
            return "🚀", "趋势成长", f"动量{mom_r:.0f}/MACD{macd_r:.0f}强势，右侧趋势确认"
        if pe_r and pe_r < 30 and mom_r and mom_r < 30:
            return "🔄", "反转机会", f"PE前{pe_r:.0f}%低+动量{mom_r:.0f}底部，超跌修复"
        above50 = sum(1 for r in [pe_r, pb_r, roe_r, mom_r, macd_r] if r and r > 50)
        if above50 >= 3:
            return "📊", "综合偏强", f"{above50}/5因子处于市场上半区"
        return "📊", "综合评分", f"评分{score}"
    fs = _parse_fs(fd)
    if fs.get("value_discount", 0) < -1 and fs.get("roe_quality", 0) > 0.5:
        return "🏆", "深度价值", f"估值低+ROE优质"
    if fs.get("momentum_20d", 0) > 0.5 and fs.get("macd_hist", 0) > 0:
        return "🚀", "趋势成长", f"动量偏强，右侧趋势"
    if fs.get("value_discount", 0) > 0.5 and fs.get("momentum_20d", 0) < -0.5:
        return "🔄", "反转机会", f"超跌+估值修复"
    above_avg = sum(1 for k in ["momentum_20d", "roc20", "rsi14", "macd_hist"] if fs.get(k, 0) > 0)
    if above_avg >= 3:
        return "📊", "综合偏强", f"{above_avg}/4技术因子偏强"
    return "📊", "综合评分", f"评分{score}"


def render_stock(code, rec, idx):
    """渲染单只股票分析"""
    detail = fetch_stock_detail(code)
    name = detail["name"] or rec.get("name", code)
    industry = detail["industry"] or ""
    fd = detail["factor"]
    fin = detail["financial"]
    klines = detail["klines"]
    pe = detail["pe"]
    pb = detail["pb"]

    # 基础信息
    price = klines[0][1] if klines else 0
    chg = klines[0][5] if klines else 0
    icon = "🔴" if chg >= 0 else "🟢"

    pe_str = f"PE{pe:.1f}x" if pe and pe > 0 else ""
    pb_str = f"PB{pb:.1f}x" if pb else ""
    roe_str = f"ROE{fd[2]:.1f}%" if fd and fd[2] else ""
    mv_str = _fmt_mv(fd[16]) if fd and fd[16] else ""
    score = rec.get("score", 0)
    extra = [s for s in [pe_str, pb_str, roe_str, mv_str, f"评分{score}" if score else ""] if s]
    title = f"{idx}. {name}({code}) | {icon}{price:.2f}元（{chg:+.2f}%）| {' | '.join(extra)}"

    parts = []

    # 行业 & 概念
    parts.append(f"**{industry}**" if industry else "")

    # 近期表现
    perf = []
    if abs(chg) >= 3:
        perf.append(f"今日{'大涨' if chg > 0 else '大跌'}{abs(chg):.1f}%")
    elif abs(chg) >= 1:
        perf.append(f"今日{'上涨' if chg > 0 else '下跌'}{chg:+.1f}%")
    else:
        perf.append(f"今日{chg:+.2f}%")

    mom5 = fd[8] if fd else None
    if mom5 is not None:
        perf.append(f"近5日{'涨' if mom5 >= 0 else '跌'}{abs(mom5):.1f}%")

    closes = [k[1] for k in klines if k[1]]
    pp = calc_percentile(closes, price) if closes else None
    if pp is not None:
        zone = "低位区" if pp < 30 else "中位区" if pp < 70 else "高位区"
        perf.append(f"价格{zone}")

    if perf:
        parts.append("走势：" + " | ".join(perf[:4]))

    # 财务
    fin_items = []
    if fin:
        if fin[0]: fin_items.append(f"毛利率{fin[0]:.0f}%")
        if fin[1]: fin_items.append(f"净利率{fin[1]:.0f}%")
    if fin_items:
        parts.append("财务：" + " | ".join(fin_items))

    # 技术面
    tech = []
    ma_status = fd[12] if fd else None
    if ma_status:
        tech.append(f"均线{ma_status}")
    rsi6 = fd[11] if fd else None
    if rsi6 is not None:
        if rsi6 >= 70: tech.append(f"RSI({rsi6:.0f})超买区")
        elif rsi6 >= 60: tech.append(f"RSI({rsi6:.0f})偏强")
        elif rsi6 >= 40: tech.append(f"RSI({rsi6:.0f})中性")
        else: tech.append(f"RSI({rsi6:.0f})偏弱")
    elif fd:
        fs = _parse_fs(fd)
        rsi14 = fs.get("rsi14")
        if rsi14 is not None:
            pct = _z_to_pct(rsi14)
            if pct >= 70: tech.append(f"RSI超买区（{pct:.0f}%分位）")
            elif pct >= 60: tech.append(f"RSI偏强（{pct:.0f}%分位）")
            elif pct >= 40: tech.append(f"RSI中性（{pct:.0f}%分位）")
            else: tech.append(f"RSI偏弱（{pct:.0f}%分位）")
    vratio = fd[14] if fd else None
    if vratio is not None:
        if vratio > 2: tech.append(f"放量{vratio:.1f}倍")
        elif vratio > 1: tech.append(f"量能正常{vratio:.1f}倍")
        else: tech.append(f"缩量{vratio:.1f}倍")
    near_s, near_r = sr_levels(klines)
    if near_s and near_r and price:
        tech.append(f"支撑{near_s:.2f} | 压力{near_r:.2f}")
    if tech:
        parts.append("技术：" + " | ".join(tech[:5]))

    # 因子分析（优先 rank 列，回退 factor_scores JSON）
    if fd:
        factor_items = []
        has_rank = any(fd[i] is not None for i in [3, 5, 6, 7])
        if has_rank:
            for label, rank_val, good in [("PE估值", fd[3], False), ("ROE", fd[5], True),
                                           ("动量", fd[6], True), ("MACD", fd[7], True)]:
                desc = rank_desc(label, rank_val, good)
                if desc:
                    factor_items.append(desc)
        else:
            fs = _parse_fs(fd)
            fs_labels = [("估值", "value_discount", False), ("ROE质量", "roe_quality", True),
                         ("动量", "momentum_20d", True), ("短期动量", "roc5", True)]
            for label, key, good in fs_labels:
                z = fs.get(key)
                if z is not None:
                    pct = _z_to_pct(z)
                    desc = rank_desc(label, pct, good)
                    if desc:
                        factor_items.append(desc)
        if factor_items:
            parts.append("因子：" + " | ".join(factor_items[:3]))

    # 股票类型 + 结论
    emoji, st_name, st_desc = classify_stock_type(fd, fin, score)
    verdict = f"{emoji} {st_name}：{st_desc}"
    if ma_status and "空头" in str(ma_status):
        verdict += "，注意均线空头排列风险"
    parts.append("→ " + verdict)

    # 操作参考
    action = []
    if near_s and price:
        buy_range = f"{near_s:.2f}-{round(price * 1.02, 2)}"
        action.append(f"区间：{buy_range}")
    if near_s and price:
        stop_v = round(near_s * 0.95, 2)
        stop_pct = (stop_v - price) / price * 100
        action.append(f"止损：{stop_v:.2f}（{stop_pct:.1f}%）")
    if action:
        parts.append(f"\n**操作参考：** {' | '.join(action)}")

    return f"**{title}**\n\n" + "\n".join(parts)


# ============================================================
# AI 市场评论
# ============================================================

def build_ai_commentary(bs, inds, lhb_text):
    """LLM 市场综合评论"""
    if not bs:
        return None
    prompt = f"""基于以下A股盘面数据，给出简洁的市场综合判断（2-3句话）：
涨跌比：上涨{bs['up']}/下跌{bs['dn']}，均值{bs['avg']:+.2f}%
涨停{bs['zt']}只/跌停{bs['dt']}只
"""
    if inds:
        top3 = "、".join(i[0] for i in inds[:3])
        bot3 = "、".join(i[0] for i in sorted(inds, key=lambda x: x[2])[:3])
        prompt += f"最强板块：{top3}\n最弱板块：{bot3}\n"
    if lhb_text:
        prompt += f"龙虎榜：{lhb_text[:200]}\n"
    prompt += "\n请给出：【结论】一句话判断 + 【分析】核心逻辑 + 【风险】主要风险点"
    result = _llm_call(prompt, timeout_s=8, max_tokens=2000, point="evening_ai")
    if result:
        result = re.sub(r'#{1,6}\s*\*?', '', result.strip())
    return result


# ============================================================
# 构建 Card 1: 盘面总览
# ============================================================

def build_card1():
    """盘面总览"""
    sections = []
    latest = fetch_latest_dates()
    kline_date = latest["kline"]

    # 指数表现
    indices = fetch_indices()
    sections.append({
        "title": "📈 指数表现",
        "body": fmt_indices(indices),
        "subtitle": "实时行情",
    })

    # 涨跌分布 + 核心判断
    bs = fetch_market_snapshot(kline_date)
    inds = fetch_industry_perf(kline_date)
    if bs and bs["total"] > 0 and bs["up"] + bs["dn"] > 0:
        sections.append({
            "title": "📊 涨跌分布",
            "body": f"均值{bs['avg']:+.2f}% | 上涨{bs['up']}/下跌{bs['dn']} | 涨停{bs['zt']}/跌停{bs['dt']} | 数据日期 {bs.get('date', kline_date)}",
        })
        sections.append({
            "title": "🎯 核心判断",
            "body": fmt_market_judgment(bs, inds),
        })
    elif bs and bs["total"] > 0 and bs["up"] + bs["dn"] == 0:
        sections.append({
            "title": "⚠️ 涨跌分布",
            "body": f"数据日期 {kline_date} 的 K 线数据中 change_pct 字段暂未填充（{bs['total']} 条记录），涨跌分布不可用。数据管线将在下次运行时自动补全。",
            "subtitle": "数据质量提示",
        })
    else:
        sections.append({
            "title": "⚠️ 涨跌分布",
            "body": f"数据日期 {kline_date} 无 K 线数据。请检查数据管线是否正常运行。",
            "subtitle": "数据缺失提示",
        })

    # 板块轮动
    if inds:
        sections.append({
            "title": "🔄 板块轮动",
            "body": fmt_industry(inds),
        })

    # 龙虎榜
    lhb = fetch_lhb_data()
    lhb_text = fmt_lhb(lhb)
    if lhb_text:
        sections.append({
            "title": "🐉 龙虎榜",
            "body": lhb_text,
            "subtitle": f"数据日期 {lhb.get('date', DATE)}",
        })

    # 风险监控（仅当有真实数据时）
    if bs and bs["up"] + bs["dn"] > 0:
        sections.append({
            "title": "⚠️ 风险监控",
            "body": fmt_risk(bs, inds),
        })

    # AI 市场评论（仅当有真实数据时）
    if bs and bs["up"] + bs["dn"] > 0:
        ai = build_ai_commentary(bs, inds, lhb_text or "")
        if ai:
            sections.append({
                "title": "🤖 AI 市场评论",
                "body": ai,
                "subtitle": "LLM 基于盘面数据综合分析",
            })

    # 明日展望
    if inds and any(i[2] != 0 for i in inds):
        sections.append({
            "title": "🔮 明日关注",
            "body": fmt_tomorrow(inds),
        })
    elif inds:
        sections.append({
            "title": "🔮 明日关注",
            "body": "行业涨跌幅数据暂未填充，无法提供板块轮动分析。",
        })

    # footer
    now = bj_now()[:19]
    sections.append({"title": "", "body": f"<font color='grey'>QuantStar 量化投研系统 | {now} | 仅供参考</font>"})

    return {"sections": sections, "date": DATE}


# ============================================================
# 构建 Card 2: 个股精选
# ============================================================

def build_card2():
    """个股精选"""
    sections = []

    # 持仓跟踪（pending_entry 显示为待入场）
    positions = fetch_positions()
    if positions:
        pos_lines = []
        for p in positions:
            display_name = p["name"] or p["code"]
            if p["status"] == "pending_entry":
                pos_lines.append(
                    f"⏳ {display_name}({p['code']}) 待入场 | 评分{p['score']:.1f} | {p['industry']}"
                )
            elif p["entry"]:
                icon = "🔴" if p["pnl_pct"] >= 0 else "🟢"
                pos_lines.append(
                    f"{icon} {display_name}({p['code']}) {p['pnl_pct']:+.1f}% | "
                    f"持有{p['days']}天 | 评分{p['score']:.1f} | {p['industry']}"
                )
        if pos_lines:
            sections.append({
                "title": f"💼 持仓跟踪（{len(positions)}只）",
                "body": "\n".join(pos_lines),
            })
    else:
        sections.append({
            "title": "💼 持仓跟踪",
            "body": "当前无有效持仓",
        })

    # 今日操作（仅显示 entry/exit，过滤 status_change）
    actions = fetch_today_actions()
    key_actions = [a for a in actions if a["type"] in ("entry", "exit", "adjust")]
    if key_actions:
        act_lines = []
        for a in key_actions:
            type_map = {"entry": "🟢买入", "exit": "🔴卖出", "adjust": "🟡调整"}
            type_str = type_map.get(a["type"], a["type"])
            act_lines.append(f"{type_str} {a['name']}({a['code']}) {a['price'] or '--'}元")
        sections.append({
            "title": "📋 今日操作",
            "body": "\n".join(act_lines),
        })

    # 个股精选（从 pending_entry 中取，优先有 score 的）
    watch = fetch_watch_list()
    pending = [w for w in watch if w.get("score", 0) > 0][:5]
    if not pending:
        pending = [w for w in watch if w.get("name")][:5]
    if pending:
        sections.append({
            "title": f"📌 个股精选（TOP {len(pending)}）",
            "body": f"从待入场池中按评分排序",
            "subtitle": "因子评分 + 技术面 + 基本面综合分析",
        })
        for i, w in enumerate(pending, 1):
            text = render_stock(w["code"], w, i)
            if text:
                sections.append({"body": text})
    elif watch:
        sections.append({
            "title": "📌 个股精选",
            "body": f"待入场池 {len(watch)} 只，但均无评分数据，无法提供分析",
        })

    # 昨日推荐回顾
    yr = fetch_yesterday_review()
    if yr and yr["stocks"]:
        yr_lines = []
        for s in yr["stocks"]:
            icon = "🔴" if s["chg"] and s["chg"] >= 0 else "🟢"
            chg_str = f"{s['chg']:+.2f}%" if s["chg"] is not None else "待更新"
            yr_lines.append(f"{icon} {s['name']}({s['code']}): {chg_str}")
        win_rate = yr["wins"] / yr["total"] * 100 if yr["total"] else 0
        sections.append({
            "title": f"📈 昨日推荐回顾",
            "body": f"昨日推荐 {yr['total']}只 | 上涨{yr['wins']}/{yr['total']} 胜率{win_rate:.0f}% | 平均{yr['avg_chg']:+.2f}%\n" + "\n".join(yr_lines),
        })

    # 关注列表
    watch_only = [w for w in watch if w.get("score", 0) > 0][5:10]
    if watch_only:
        wl_lines = [f"👀 {w['name']}({w['code']}) 评分{w['score']:.1f} | {w['industry']}" for w in watch_only]
        sections.append({
            "title": "📋 关注列表",
            "body": "\n".join(wl_lines),
        })

    # footer
    now = bj_now()[:19]
    sections.append({"title": "", "body": f"<font color='grey'>QuantStar 量化投研系统 | {now} | 仅供参考</font>"})

    return {"sections": sections, "date": DATE}


# ============================================================
# 主函数
# ============================================================

def push_card(content, title, dry_run=False):
    """推送一张卡片"""
    msg = Message(content=content, content_type="evening_report", title=title)
    msg.content = FeishuCardFormatter().format(msg)
    if dry_run:
        print(f"\n{'='*50}")
        print(f"{title}")
        print(f"{'='*50}")
        for s in content["sections"]:
            t = s.get('title', '')
            if t: print(f"\n{t}")
            b = s.get('body', '')
            if b: print(b)
        return True
    config = Config(); config.load()
    dl = DeliveryLayer(config=config)
    dl.registry.mark_healthy("feishu_dm")
    dl.registry.mark_healthy("feishu_group")
    results = dl.send(msg)
    items = results if isinstance(results, list) else [results]
    ok = any(getattr(r, 'status', '') == 'success' for r in items if r)
    for r in items:
        print(f"推送 [{getattr(r,'channel','?')}]: {getattr(r,'status','?')}")
    return ok


def main():
    dry_run = "--dry-run" in sys.argv
    print(f"构建晚报: {DATE} (dry_run={dry_run})")

    # Card 1: 盘面总览
    print("[1/2] 构建盘面总览…")
    c1 = build_card1()
    ok1 = push_card(c1, f"📊 晚报·盘面总览 | {_display_date(DATE)}", dry_run)

    # Card 2: 个股精选
    print("[2/2] 构建个股精选…")
    c2 = build_card2()
    ok2 = push_card(c2, f"🎯 晚报·个股精选 | {_display_date(DATE)}", dry_run)

    if dry_run:
        print(f"\n⏸️  dry-run 模式，未推送")
    elif ok1 and ok2:
        print("✅ 晚报推送完成")
    else:
        print(f"⚠️ 推送结果: card1={ok1}, card2={ok2}")


if __name__ == "__main__":
    main()

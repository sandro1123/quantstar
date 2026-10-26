"""构建午间速报 — 差异化早报，聚焦上午复盘+下午策略"""
import sys, os, re, concurrent.futures
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(project_root)
load_dotenv(os.path.join(project_root, ".env"))

from common.utils import bj_date, bj_now
from common.logging import get_logger
from delivery_layer import DeliveryLayer, Message
from report_layer.formatters.feishu_card import FeishuCardFormatter
from config_layer import Config
from data_layer.news_collector import NewsCollector
from agent_layer.llm.service import LLMService
from agent_layer.llm.provider_manager import ProviderManager

logger = get_logger("scripts.midday_brief")
DATE = bj_date()
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


def _llm_call(prompt, timeout_s=20, max_tokens=2000, point="llm"):
    llm = _init_llm()
    if not llm or not llm.is_available():
        return None
    def _do():
        return llm.call(prompt, temperature=0.5, max_tokens=max_tokens, call_point=point)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_do)
            return fut.result(timeout=timeout_s)
    except Exception:
        return None


# ============================================================
# 1. 上午盘面回顾
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
    up = sum(1 for i in indices if i["pct"] > 0)
    down = sum(1 for i in indices if i["pct"] < 0)
    lines.append(f"\n上午{'上涨' if up > down else '下跌'}为主，{up}涨{down}跌")
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
# 2. 板块轮动 — Fuyao 板块指数
# ============================================================
def build_sectors():
    import requests
    api_key = os.environ.get("FUYAO_API_KEY", "")
    headers = {"X-api-key": api_key}
    try:
        resp = requests.get("https://fuyao.aicubes.cn/api/a-share-index/catalog/ths-index-list?tag=industry",
                            headers=headers, timeout=8)
        if resp.status_code != 200: return []
        data = resp.json()
        if data.get("code") != 0: return []
        items = data.get("data", {}).get("item", [])
        top_thscodes = [item["thscode"] for item in items[:80]]
        codes_str = ",".join(top_thscodes)
        if not codes_str: return []
        snap = requests.get(f"https://fuyao.aicubes.cn/api/a-share-index/prices/snapshot?thscodes={codes_str}",
                            headers=headers, timeout=8)
        if snap.status_code != 200: return []
        snap_data = snap.json()
        if snap_data.get("code") != 0: return []
        name_map = {item["thscode"]: item["name"] for item in items}
        sectors = []
        for item in snap_data.get("data", {}).get("item", []):
            ts = item.get("thscode", "")
            name = name_map.get(ts, ts)
            pct = item.get("price_change_ratio_pct", 0) or 0
            sectors.append({"name": name, "pct": round(pct, 2)})
        sectors.sort(key=lambda x: x["pct"], reverse=True)
        return sectors[:10] + sectors[-10:]  # top 10 + bottom 10
    except Exception as e:
        logger.warning(f"Sectors failed: {e}")
        return []


def format_sectors(sectors):
    if not sectors: return "暂无板块数据"
    up = sorted([s for s in sectors if s["pct"] > 0], key=lambda x: x["pct"], reverse=True)
    down = sorted([s for s in sectors if s["pct"] < 0], key=lambda x: x["pct"])
    lines = []
    if up:
        lines.append("涨幅板块:")
        for s in up[:5]:
            lines.append(f"  {s['name']}: 🔴 {s['pct']:+.2f}%")
    else:
        lines.append("涨幅板块: 无")
    if down:
        lines.append("\n跌幅板块:")
        for s in down[:5]:
            lines.append(f"  {s['name']}: 🟢 {s['pct']:+.2f}%")
    return "\n".join(lines)


# ============================================================
# 3. 上午异动
# ============================================================
def build_alerts(date):
    try:
        from data_layer.sources.fuyao_client import FuyaoClient
        client = FuyaoClient()
        data = client.get_limit_up_pool(date)
        if data:
            up = [d for d in data if d.get("type") == "涨停"][:5]
            down = [d for d in data if d.get("type") == "跌停"][:5]
            return up, down
    except Exception:
        pass
    return [], []


def format_alerts(up, down):
    lines = []
    if up:
        lines.append(f"🔴 涨停 ({len(up)} 只)")
        for item in up[:5]:
            lines.append(f"  {item.get('name', item.get('thscode', ''))}")
    if down:
        lines.append(f"\n🟢 跌停 ({len(down)} 只)")
        for item in down[:5]:
            lines.append(f"  {item.get('name', item.get('thscode', ''))}")
    if not up and not down:
        lines.append("暂无涨跌停数据")
    return "\n".join(lines)


# ============================================================
# 4. 午间要闻
# ============================================================
def build_news(date):
    try:
        llm = _init_llm()
        collector = NewsCollector(llm_service=llm)
        collector.collect(date)
        items = collector.get_news(date, limit=5)
        return items[:3]
    except Exception as e:
        logger.warning(f"News failed: {e}")
        return []


def format_news(items):
    if not items: return "暂无重要新闻"
    lines = []
    for i, item in enumerate(items[:3], 1):
        title = item.get("title", "")
        summary = item.get("summary", "").strip()[:60]
        lines.append(f"{i}. {title}")
        if summary:
            lines.append(f"   {summary}")
    return "\n".join(lines)


# ============================================================
# 5. 下午策略 (LLM)
# ============================================================
def build_strategy(indices, sectors):
    if not indices: return None
    index_text = "; ".join(f"{i['name']}: {i['price']} ({i['pct']:+.2f}%)" for i in indices[:3])
    sector_text = ""
    if sectors:
        top = sorted(sectors, key=lambda x: x["pct"], reverse=True)[:3]
        bottom = sorted(sectors, key=lambda x: x["pct"])[:3]
        sector_text = "强势板块: " + "; ".join(f"{s['name']}({s['pct']:+.2f}%)" for s in top)
        sector_text += " | 弱势板块: " + "; ".join(f"{s['name']}({s['pct']:+.2f}%)" for s in bottom)
    prompt = (
        f"今日上午A股收盘数据:\n{index_text}\n"
        f"{sector_text}\n\n"
        f"请分析上午盘面，给出下午策略，用以下格式，不要markdown:\n"
        f"【结论】一句话总结下午方向\n"
        f"【分析】2-3句关键分析\n"
        f"【板块】关注和回避的板块\n"
        f"【操作】具体操作建议"
    )
    r = _llm_call(prompt, timeout_s=20, max_tokens=2000, point="midday_strategy")
    if r:
        return re.sub(r'#{1,6}\s*\*?', '', r.strip())
    up = sum(1 for i in indices if i["pct"] > 0)
    direction = "偏多" if up > len(indices) / 2 else "偏空" if up < len(indices) / 2 else "震荡"
    return f"方向判断: {direction}\n建议仓位: 5-7成"


def format_strategy(body):
    return body or "研判暂不可用"


# ============================================================
# 构建
# ============================================================
def build_midday_brief(date):
    sections = []

    indices = build_indices()
    idx_body, idx_columns = format_indices(indices)
    section = {"title": "📊 上午盘面", "body": idx_body,
               "subtitle": "腾讯财经实时行情 · 11:30 上午收盘"}
    if idx_columns:
        section["columns"] = idx_columns
    sections.append(section)

    sectors = build_sectors()
    sections.append({"title": "🎯 板块轮动", "body": format_sectors(sectors),
                     "subtitle": "Fuyao 同花顺行业指数 · 上午涨跌排行"})

    up, down = build_alerts(date)
    sections.append({"title": "⚡ 上午异动", "body": format_alerts(up, down),
                     "subtitle": "Fuyao 涨跌停数据"})

    news = build_news(date)
    sections.append({"title": "📰 午间要闻", "body": format_news(news),
                     "subtitle": "东方财富新闻源 · 上午重要资讯"})

    strategy = build_strategy(indices, sectors)
    sections.append({"title": "🧠 下午策略", "body": format_strategy(strategy),
                     "subtitle": "LLM 基于上午盘面+板块轮动的综合分析"})

    now = bj_now()[:19]
    sections.append({"title": "", "body": f"<font color='grey'>QuantStar 量化投研系统 | {now} | 仅供参考</font>"})

    return {"sections": sections, "date": date}


def main():
    dry_run = "--dry-run" in sys.argv
    logger.info(f"构建午报: {DATE} (dry_run={dry_run})")
    content = build_midday_brief(DATE)

    print(f"\n{'='*50}")
    print(f"☀️ QuantStar 午间速报 — {DATE}")
    print(f"{'='*50}")
    for s in content["sections"]:
        print(f"\n{s.get('title', '')}")
        if s.get('subtitle'): print(f"  {s['subtitle']}")
        print(s.get('body', ''))
    print(f"\n{'='*50}")

    output_dir = os.path.join("output", "preview")
    os.makedirs(output_dir, exist_ok=True)
    save_path = os.path.join(output_dir, f"midday_brief_{DATE}.txt")
    with open(save_path, "w", encoding="utf-8") as f:
        for s in content["sections"]:
            f.write(f"--- {s['title']} ---\n")
            if s.get('subtitle'): f.write(f"  {s['subtitle']}\n")
            f.write(f"{s['body']}\n\n")
    logger.info(f"预览已保存: {save_path}")

    if dry_run:
        print(f"\n⏸️  dry-run 模式，未推送。查看: {save_path}")
        return

    config = Config(); config.load()
    msg = Message(content=content, content_type="midday_brief", title=f"☀️ QuantStar 午间速报 — {DATE}")
    msg.content = FeishuCardFormatter().format(msg)
    dl = DeliveryLayer(config=config)
    dl.registry.mark_healthy("feishu_dm")
    dl.registry.mark_healthy("feishu_group")
    for r in (dl.send(msg) if isinstance(dl.send(msg), list) else [dl.send(msg)]):
        print(f"推送 [{getattr(r,'channel','?')}]: {getattr(r,'status','?')}")
    print(f"已推送")


if __name__ == "__main__":
    main()
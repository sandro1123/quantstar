"""NewsCollector — 新闻采集与持久化，支持多源获取、去重、情感分类、行业标签"""

from __future__ import annotations

import difflib
from typing import Any

from common.database import Database
from common.logging import get_logger

logger = get_logger("data.news")

SECTOR_KEYWORDS: dict[str, list[str]] = {
    "新能源": ["新能源", "光伏", "锂电池", "风电", "储能", "氢能", "太阳能", "电池", "锂电", "硅料"],
    "科技": ["科技", "AI", "人工智能", "芯片", "半导体", "5G", "软件", "互联网", "大数据", "云计算", "算法", "大模型", "机器人", "算力", "信创", "数字经济"],
    "医药": ["医药", "医疗", "生物", "疫苗", "创新药", "中药", "医疗器械", "医保", "CXO", "cro", "医美"],
    "金融": ["金融", "银行", "保险", "证券", "券商", "基金", "信托", "理财", "保险", "期货", "投行"],
    "消费": ["消费", "白酒", "食品", "饮料", "零售", "电商", "免税", "餐饮", "旅游", "家电", "纺织", "服装", "珠宝"],
    "地产": ["地产", "房地产", "基建", "建筑", "建材", "装修", "物业", "保障房", "城投"],
    "汽车": ["汽车", "新能源车", "自动驾驶", "整车", "零部件", "充电桩", "特斯拉", "比亚迪"],
    "周期": ["钢铁", "煤炭", "有色", "化工", "石油", "天然气", "黄金", "大宗商品", "原油", "铜", "铝", "镍"],
    "制造业": ["制造", "工业", "机械", "装备", "军工", "航天", "航空", "船舶", "激光"],
    "通信": ["通信", "6G", "卫星", "光通信", "电信", "北斗", "星链"],
    "公用事业": ["电力", "水务", "燃气", "环保", "碳中和", "碳交易", "节能减排"],
    "农林牧渔": ["农业", "种业", "猪肉", "粮食", "农产品", "养殖", "渔业", "林业", "牧业"],
    "教育": ["教育", "培训", "在线教育", "职业教育"],
    "传媒": ["传媒", "游戏", "影视", "广告", "元宇宙", "NFT", "直播", "短视频"],
}

BULLISH_KEYWORDS: list[str] = [
    "大涨", "上涨", "利好", "突破", "反弹", "增长", "创新高", "爆发",
    "涨停", "拉升", "强势", "看涨", "看好", "买入", "增持", "回购",
    "放量", "领涨", "飙升", "井喷", "供不应求", "超预期", "扭亏",
    "提振", "加仓", "做多", "反转", "复苏", "扩张", "加速", "新高",
    "放量突破", "触底反弹", "边际改善", "景气上行",
]

BEARISH_KEYWORDS: list[str] = [
    "大跌", "下跌", "利空", "跌破", "回调", "下降", "新低", "跌停",
    "跳水", "弱势", "看跌", "卖出", "减持", "缩量", "领跌", "风险",
    "警告", "亏损", "爆雷", "违约", "退市", "做空", "崩盘", "恐慌",
    "下调", "恶化", "萎缩", "ST", "*ST", "退市警告", "立案", "处罚",
    "裁员", "降薪", "资金链断裂", "债务违约", "立案调查", "整顿",
    "停牌", "暂停上市", "保证金", "强制平仓",
]

NOISE_KEYWORDS: list[str] = [
    "美元指数", "美联储", "鲍威尔", "华尔街",
    "欧洲央行", "日本央行", "英国", "德国", "法国",
    "伊朗", "中东", "霍尔木兹", "波斯湾",
    "OPEC", "欧佩克",
    "体育", "NBA", "英超", "欧冠", "奥运会",
    "好莱坞", "奥斯卡", "格莱美",
    "天气预报", "恒指", "恒生", "港股",
    "期货研报", "期货早报", "期货日报", "原油早报",
]

A_STOCK_KEYWORDS: list[str] = [
    "A股", "沪指", "深指", "创业板", "科创板", "北交所",
    "主力资金", "涨停", "跌停", "成交额",
    "证监会", "交易所", "IPO", "注册制", "退市",
    "降准", "降息", "LPR", "MLF", "逆回购",
    "人民币", "汇率",
    "半导体", "芯片", "新能源", "光伏", "风电", "储能",
    "汽车", "医疗", "医药", "消费", "白酒", "金融",
    "地产", "银行", "券商", "保险",
    "AI", "人工智能", "概念", "利好", "利空",
    "上市公司", "板块", "产业链",
]


class NewsCollector:
    """新闻采集器，从多个 AkShare 源采集新闻并持久化到 stock_news"""

    def __init__(self, db: Database | None = None, llm_service=None):
        self.db = db or Database()
        self.llm = llm_service
        self._attributor = None  # P2-19 Phase 0: 词表归属惰性初始化
        self._ensure_table()

    def _get_attributor(self):
        """P2-19 Phase 0: 词表归属器惰性加载 (词表加载失败静默降级, 不阻断采集)"""
        if self._attributor is None:
            try:
                from data_layer.news_attribution import NewsAttributor
                self._attributor = NewsAttributor(self.db)
                if self._attributor.load_vocab() == 0:
                    self._attributor = None
            except Exception as e:
                logger.warning(f"News attribution init failed: {e}")
                self._attributor = None
        return self._attributor

    def _ensure_table(self):
        conn = self.db.get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_news (
                code TEXT,
                date TEXT,
                title TEXT,
                source TEXT,
                sentiment TEXT,
                sentiment_score REAL,
                summary TEXT
            )
        """)
        # P2-19 Phase 0: 旧表补 attribution_method 列 (CREATE IF NOT EXISTS 不升级)
        existing = {r[1] for r in conn.execute("PRAGMA table_info(stock_news)")}
        if "attribution_method" not in existing:
            conn.execute("ALTER TABLE stock_news ADD COLUMN attribution_method TEXT")
        conn.commit()

    def collect(self, date: str, codes: list[str] | None = None) -> int:
        items: list[dict[str, Any]] = []
        items.extend(self._fetch_global_em(date))
        items.extend(self._fetch_caixin(date))
        items.extend(self._fetch_cctv(date))
        # AG-NEWS ④a: 个股新闻腿 (stock_news_em 东财域, 逐股 10 行)。
        # codes=None 不拉 (防全市场打爆源); 候选池经管道夜窗传入。
        if codes:
            items.extend(self._fetch_per_stock_em(codes))
        items = self._deduplicate(items)
        items = self._filter_noise(items)
        if self.llm and len(items) > 5:
            items = self._llm_quality_filter(items)
        for item in items:
            label, score = self._classify(item.get("title", ""), item.get("summary", ""))
            item["sentiment"] = label
            item["sentiment_score"] = score
            item["sector"] = self._tag_sector(item.get("title", ""))
        self._persist(items)
        logger.info(f"Collected {len(items)} news items for {date}")
        return len(items)

    def get_news(self, date: str, since_time: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        conn = self.db.get_connection()
        if since_time:
            since_dt = f"{date} {since_time}"
            rows = conn.execute(
                "SELECT code, date, title, source, sentiment, sentiment_score, summary FROM stock_news "
                "WHERE date >= ? AND date LIKE ? ORDER BY date DESC LIMIT ?",
                (since_dt, f"{date}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT code, date, title, source, sentiment, sentiment_score, summary FROM stock_news "
                "WHERE date LIKE ? ORDER BY date DESC LIMIT ?",
                (f"{date}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_global_em(self, date: str) -> list[dict[str, Any]]:
        try:
            import akshare as ak
            df = ak.stock_info_global_em()
            items = []
            for _, row in df.iterrows():
                title = str(row.get("标题", row.get("title", ""))).strip()
                summary = str(row.get("摘要", row.get("summary", ""))).strip()
                pub_time = str(row.get("发布时间", row.get("date", ""))).strip()[:19]
                if not title:
                    continue
                items.append({
                    "code": "",
                    "title": title,
                    "summary": summary,
                    "date": pub_time or date,
                    "source": "东方财富",
                })
            logger.info(f"Fetched {len(items)} items from stock_info_global_em")
            return items
        except Exception as e:
            logger.warning(f"stock_info_global_em failed: {e}")
            return []

    def _fetch_caixin(self, date: str) -> list[dict[str, Any]]:
        try:
            import akshare as ak
            df = ak.stock_news_main_cx()
            items = []
            for _, row in df.iterrows():
                title = str(row.get("标题", row.get("title", ""))).strip()
                summary = str(row.get("内容", row.get("summary", row.get("摘要", "")))).strip()
                pub_time = str(row.get("发布时间", row.get("date", ""))).strip()[:19]
                if not title:
                    continue
                items.append({
                    "code": "",
                    "title": title,
                    "summary": summary[:200],
                    "date": pub_time or date,
                    "source": "财新",
                })
            logger.info(f"Fetched {len(items)} items from stock_news_main_cx")
            return items
        except Exception as e:
            logger.warning(f"stock_news_main_cx failed: {e}")
            return []

    def _fetch_cctv(self, date: str) -> list[dict[str, Any]]:
        try:
            import akshare as ak
            df = ak.news_cctv(date=date.replace("-", ""))
            items = []
            for _, row in df.iterrows():
                title = str(row.get("title", "")).strip()
                if not title:
                    continue
                items.append({
                    "code": "",
                    "title": title,
                    "summary": "",
                    "date": date,
                    "source": "CCTV",
                })
            logger.info(f"Fetched {len(items)} items from news_cctv")
            return items
        except Exception as e:
            logger.warning(f"news_cctv failed: {e}")
            return []

    def _fetch_per_stock_em(self, codes: list[str]) -> list[dict[str, Any]]:
        """AG-NEWS ④a: akshare stock_news_em 个股新闻腿 (东财域主通道)。

        每股 ~10 行; 单股异常 (429 限流/停牌无稿) 跳过不炸腿 — 降级记档
        停工不硬刚 (派单纪律)。attribution_method='per_stock_em' 可辨于
        派生关联。节流: 股间 sleep 1s (夜窗首真拉面, 限频内轻拉)。
        """
        import time as _time
        items: list[dict[str, Any]] = []
        try:
            import akshare as ak
        except Exception as e:
            logger.warning(f"akshare import failed (per-stock leg skipped): {e}")
            return items
        for code in codes:
            try:
                df = ak.stock_news_em(symbol=code)
                for _, row in df.iterrows():
                    title = str(row.get("新闻标题", "")).strip()
                    if not title:
                        continue
                    pub = str(row.get("发布时间", "")).strip()[:19]
                    items.append({
                        "code": code,
                        "title": title,
                        "summary": str(row.get("新闻内容", "")).strip()[:200],
                        "date": pub or "",
                        "source": str(row.get("文章来源", "东财个股")).strip(),
                        "attribution_method": "per_stock_em",
                    })
            except Exception as e:
                logger.warning(f"stock_news_em({code}) failed, skip: {e}")
            _time.sleep(1)
        logger.info(f"per-stock leg: {len(items)} items for {len(codes)} codes")
        return items

    def _deduplicate(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for item in items:
            title = (item.get("title") or "").strip().lower()
            if not title:
                continue
            is_dup = False
            for existing in kept:
                etitle = (existing.get("title") or "").strip().lower()
                if difflib.SequenceMatcher(None, title, etitle).ratio() > 0.85:
                    is_dup = True
                    break
            if not is_dup:
                kept.append(item)
        return kept

    def _classify(self, title: str, summary: str) -> tuple[str, float]:
        text = (title + " " + summary).lower()
        bullish_score = sum(1 for kw in BULLISH_KEYWORDS if kw.lower() in text)
        bearish_score = sum(1 for kw in BEARISH_KEYWORDS if kw.lower() in text)
        if bullish_score > bearish_score:
            return "bullish", min(100.0, 50.0 + bullish_score * 10.0)
        elif bearish_score > bullish_score:
            return "bearish", max(0.0, 50.0 - bearish_score * 10.0)
        else:
            return "neutral", 50.0

    def _tag_sector(self, title: str) -> str:
        text = title.lower()
        for sector, keywords in SECTOR_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text:
                    return sector
        return ""

    def _filter_noise(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """噪音过滤：剔除非A股相关新闻"""
        filtered = []
        for item in items:
            text = ((item.get("title") or "") + " " + (item.get("summary") or "")).lower()
            noise_score = sum(1 for kw in NOISE_KEYWORDS if kw.lower() in text)
            a_stock_score = sum(1 for kw in A_STOCK_KEYWORDS if kw.lower() in text)
            if a_stock_score >= 1 or noise_score == 0 or a_stock_score >= noise_score:
                filtered.append(item)
            else:
                logger.debug(f"Noise filtered: {item.get('title', '')[:40]}")
        return filtered

    def _llm_quality_filter(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """LLM 质量过滤：用大模型判断新闻是否与 A 股投资相关"""
        if not self.llm or not self.llm.is_available():
            return items
        titles = [f"{i+1}. {(item.get('title') or '')[:60]}" for i, item in enumerate(items[:15])]
        prompt = (
            "以下是一批新闻标题，请判断每条是否与A股投资有直接关系（政策、行业、公司业绩、资金面等）。\n"
            "对每条输出序号+Y(相关)或N(无关)，格式如: 1.Y 2.N 3.Y\n\n"
            + "\n".join(titles)
        )
        try:
            import concurrent.futures
            def _do():
                return self.llm.call(prompt, temperature=0.1, max_tokens=2000, call_point="news_quality")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_do)
                result = fut.result(timeout=15)
            if not result:
                return items
            keep = set()
            for token in result.replace(",", " ").replace("，", " ").split():
                token = token.strip()
                if "." in token or "、" in token:
                    parts = token.replace(".", " ").replace("、", " ").split()
                    if len(parts) == 2:
                        try:
                            idx = int(parts[0]) - 1
                            if parts[1].upper().startswith("Y") and 0 <= idx < len(items):
                                keep.add(idx)
                        except ValueError:
                            pass
            if not keep:
                logger.debug("LLM quality filter returned no Y marks, keeping all")
                return items
            filtered = [items[i] for i in sorted(keep)]
            logger.info(f"LLM quality filter: {len(items)} → {len(filtered)} items")
            return filtered
        except Exception as e:
            logger.warning(f"LLM quality filter failed: {e}")
            return items

    def _persist(self, items: list[dict[str, Any]]):
        conn = self.db.get_connection()
        attributor = self._get_attributor()
        inserted = 0
        attributed = 0
        for item in items:
            try:
                # 统一 ISO date 格式 YYYY-MM-DD(剔 datetime 尾巴)
                raw_date = item.get("date", "")
                if raw_date and len(str(raw_date)) > 10:
                    raw_date = str(raw_date)[:10]
                # P2-19 Phase 0: 无 code 的条目走词表归属 (宁漏勿错, 未命中留空)
                code = item.get("code", "")
                method = item.get("attribution_method") if code else None
                if not code and attributor:
                    res = attributor.attribute(item.get("title", ""), item.get("summary", ""))
                    if res:
                        code, method = res.code, res.method
                        attributed += 1
                conn.execute(
                    "INSERT OR IGNORE INTO stock_news (code, date, title, source, sentiment, sentiment_score, summary, attribution_method) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        code,
                        raw_date,
                        item.get("title", ""),
                        item.get("source", ""),
                        item.get("sentiment", "neutral"),
                        item.get("sentiment_score", 50.0),
                        item.get("summary", "")[:200],
                        method,
                    ),
                )
                inserted += 1
            except Exception as e:
                logger.warning(f"Failed to persist news item: {e}")
        conn.commit()
        logger.info(f"Persisted {inserted}/{len(items)} news items (词表归属 {attributed})")

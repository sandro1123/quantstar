"""AG-NEWS ⑤a: 新闻价值分级 — 四桶 (公告>异动解读>题材>噪声)。

设计口径 (AG-NEWS 派单⑤ + expert_news.txt 模板同源分级):
  tier 1 公告类: 公司硬信息 (回购/减持/并购/业绩预告/股权/中标/问询/停复牌)
  tier 2 异动解读: 涨停/连板/龙虎榜/资金追捧原因解读
  tier 3 题材资讯: 板块/概念催化 (泛行业但有投资增量)
  tier 4 噪声快讯: 宏观外围/例行报道 (消费面过滤掉, 不进专家脑)

关键词规则式 (与 news_collector._classify 同族风格), 宁缺毋假:
  无命中 → tier 3 (题材) 兜底? 不 — 兜底给 tier 4 (噪声), 让
  低价值默认被过滤, 只有命中规则的才升桶 (防泛新闻全进专家脑)。

classify 返回 {"tier": int, "label": str, "signals": [命中的词]}。
"""
from __future__ import annotations

import re

TIER_LABELS = {1: "公告", 2: "异动解读", 3: "题材资讯", 4: "噪声快讯"}

# AG-NEWS ②GO: 个股代码前缀 (界面快讯族 「创维数字000810.SZ)：…」).
_STOCK_CODE_RE = re.compile(r"\d{6}\.(?:SZ|SH|BJ)")

# tier 1 公告信号词 (公司动作硬信息)
ANNOUNCE_KEYWORDS = [
    "回购", "减持", "增持", "并购", "收购", "重组", "业绩预告", "预增", "预减",
    "扭亏", "中标", "中标金额", "签订", "合同", "定增", "增发", "配股", "分红",
    "派息", "除权", "股权激励", "解禁", "质押", "冻结", "问询函", "监管函",
    "立案", "调查", "处罚", "退市", "停牌", "复牌", "更名", "变更", "辞任",
    "辞职", "任命", "净利润同比", "营收同比", "半年报", "年报", "季报",
    # AG-NEWS ②GO (2026-09-12): 界面快讯族词补 — 240 行实测 ~40 行
    # 「个股code)：中报净利为 X」格式落 T4 误桶, 回收真公告.
    "中报", "三季报", "一季报", "净利为", "净利润为", "快讯",
]

# tier 2 异动解读信号词
ANOMALY_KEYWORDS = [
    "涨停", "跌停", "连板", "涨停原因", "封板", "炸板", "龙虎榜", "榜上",
    "异动", "拉升", "直线涨停", "闪崩", "跳水", "资金追捧", "游资", "锁仓",
    "大单", "主力资金", "净流入", "净流出", "情绪龙头", "总龙头", "空间板",
]

# tier 3 题材信号词
THEME_KEYWORDS = [
    "板块", "概念", "题材", "产业链", "催化", "政策", "规划", "试点",
    "行业", "赛道", "爆发", "风口", "活跃", "拉升", "上扬",
]

# tier 4 噪声信号词 (外围宏观/例行)
NOISE_KEYWORDS = [
    "美联储", "鲍威尔", "美股", "纳斯达克", "标普", "道琼斯", "欧洲央行",
    "日本央行", "英国", "德国", "法国", "伊朗", "中东", "原油期货",
    "黄金期货", "美元指数", "离岸人民币", "国债收益率", "例行", "通稿",
]


def classify_news_tier(title: str, summary: str = "") -> dict:
    """标题+摘要联合扫词分级。先专后泛: 1>2>3, 无命中落 4。

    公告词优先级最高 (即便同含涨停词 — "回购"+"涨停" 是公告驱动的涨停,
    公告桶信息量更高); 异动词次之; 题材词兜住板块资讯; 全无命中=噪声。
    """
    text = f"{title} {summary}"
    hits = lambda kws: [w for w in kws if w in text]

    # AG-NEWS ②GO: 个股 code 前缀正则直判 T1 — 标题含 6 位代码+.SZ/.SH/.BJ
    # = 该股硬信息播报 (界面快讯族), 最强信号优先于词表.
    if _STOCK_CODE_RE.search(title):
        return {"tier": 1, "label": TIER_LABELS[1], "signals": ["code_prefix"]}

    ann = hits(ANNOUNCE_KEYWORDS)
    if ann:
        return {"tier": 1, "label": TIER_LABELS[1], "signals": ann[:5]}

    ano = hits(ANOMALY_KEYWORDS)
    if ano:
        return {"tier": 2, "label": TIER_LABELS[2], "signals": ano[:5]}

    thm = hits(THEME_KEYWORDS)
    if thm:
        return {"tier": 3, "label": TIER_LABELS[3], "signals": thm[:5]}

    return {"tier": 4, "label": TIER_LABELS[4], "signals": []}


def filter_by_tier(items: list[dict], max_tier: int = 3) -> list[dict]:
    """消费面过滤: tier > max_tier 的不进专家脑 (默认噪声快讯被滤)。"""
    out = []
    for it in items:
        t = classify_news_tier(it.get("title", ""), it.get("summary", ""))
        if t["tier"] <= max_tier:
            it2 = dict(it)
            it2["news_tier"] = t["tier"]
            it2["news_tier_label"] = t["label"]
            out.append(it2)
    return out

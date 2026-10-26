# -*- coding: utf-8 -*-
"""新闻个股归属 — Phase 0 词表路线 (P2-19 设计稿第一章 路线A)

三级漏斗 A→C→B 的第一级: stock_info 词表匹配, 精度优先, 宁漏勿错。
两条子路线 (优先级递减):
  1. vocab_colon — 东财"公司名：标题"冒号格式, 前缀精确查词表 (精度近 100%)
  2. vocab_title — 全名子串命中 title

歧义规则: 同一文本命中 ≥2 个不同 code → 不归属 (宁漏勿错, 错配比漏配危险)。
归属结果带 method 标记可追溯; 未归属返回 None, 不造假。

#56 卡A (设计稿 vault 分层讨论/卡56-news归属误报治理设计稿-F-20260901.md):
出口闸门三规则 (统一出口, 防未来路线回归):
  G1 主语不变量 — 命中名必须出现在 title (钉死统一出口 _exit_guard;
      有它, 已砍的 vocab_summary 路线一条都进不来)。
  G2 嵌套名防伪 — title 含词表外更长实体 (负向别名表 NEGATIVE_ALIASES)
      → 拒归属。种子条目须附生产 title 实测频次/摸底记档证据 (同
      COMMON_NOUN_BLOCKLIST 纪律); 明确不用左右邻字符启发式 (实测假阳
      12.5%, 设计稿 §1.4)。
  G3 歧义守恒 — 多命中 → None (已在位, 钉不变量)。

北交所双码折叠 (统筹 GO sm-79ae07, 同板族限定):
  250 重名对 = 246 北交所换码对 (430/83x/87x 旧段 → 920xxx 新段,
  同公司新旧代码) + 3 跨板迁移对 + 1 双沉寂对。同板族同名多码不再整名
  剔除 — 折叠到 klines 终值较新者 (活码), 250 名回 vocab 漏报面收口。
  3 对跨板迁移对 (600832/600637, 833874/301192, 833994/301321) = 真歧义,
  统筹裁定排除不折叠 (sm-7e3af889); 双沉寂无方向证据保剔除 (宁漏勿错)。
  is_active/delist_date 无判别力 (生产 247/250 对双 active) — 唯 klines
  实证有效 (取证落盘 ~/qs-cfgsnap/audit/20260901-56attrgate/)。
"""
from dataclasses import dataclass

from common.database import Database
from common.logging import get_logger

logger = get_logger("data.news_attribution")

# 常见名词陷阱: 这些是真实股票代码 (机器人=300024, 太阳能=000591), 但作为普通
# 名词在新闻中高频泛用 (实测"机器人"出现 235 次标题, 绝大多数与该公司无关)。
# 子串路线禁用 (宁漏勿错); 冒号格式路线保留 (东财"公司名：标题"是公司新闻惯例)。
# 新增条目须附实测频次证据, 防止拍脑袋扩表。
COMMON_NOUN_BLOCKLIST = {"机器人", "太阳能", "太平洋", "大西洋"}
# ^ 扩种 (#56 alias卡, 2026-09-02 生产 ro 实测): 太平洋 601099 3/3 行
#   全地理误归 (东亚及太平洋央行会/东太平洋/西北太平洋台风), 大西洋
#   600558 route2 1/1 误归 (跨大西洋贸易) — 而 colon 路线 1/1 正例
#   (兴业银锡：大西洋锡业项目→000426) 依赖全词表保留, 本表只禁 route2。

# G2 负向别名表 (#56 卡A): 词表外更长实体 (嵌套含词表内名) 出现在 title
# → 拒归属。只收「真子串嵌套」实体 — 摸底 30 港股/海外主体经程序化筛查
# 不含 A 股词表名子串 (summary 路线主体, title 路线无暴露), 不入表。
# 新增条目须附生产 title 实测频次/摸底记档证据 (同 COMMON_NOUN_BLOCKLIST
# 纪律); 零频次条目不入表。
NEGATIVE_ALIASES = frozenset({
    # 生产实锤 (2026-08-28 ro 探针, 错归 002594 vocab_title):
    "比亚迪电子",   # 港股 0285.HK ⊃ 比亚迪 002594
    # 摸底记档语境族 (vault 卡56-summary路线误报摸底报告-F-20260831 手工误报样本):
    "法国兴业银行",  # ⊃ 兴业银行 601166 — 法兴加息预测新闻曾错归
})


@dataclass
class Attribution:
    code: str
    method: str  # vocab_colon / vocab_title


def _same_board(a: str, b: str) -> bool:
    """两码是否同板 = 双双落在北交所码域。跨板/非北交所同名对 (600832/600637
    东方明珠、833874/301192 泰祥股份、833994/301321 翰博高新) = 真歧义,
    统筹裁定排除不折叠 (sm-7e3af889)。

    北交所码域: 43x/83x/87x (老三板→精选层沿革) 与 920xxx (北交所新股号,
    92+旧码后三位), 域内互通 (换码迁移 = 同板)。冒烟实锤: OTHER×OTHER
    (双沪主板对) 曾被误折 (vocab 5755≠5752) — 任一码落 OTHER 即不折。
    """
    def board(code: str) -> str:
        if code.startswith(("43", "83", "87")):
            return "BJ_old"
        if code.startswith("92"):
            return "BJ_new"
        return "OTHER"

    return "OTHER" not in (board(a), board(b))


class NewsAttributor:
    def __init__(self, db: Database):
        self.db = db
        self._name2code: dict[str, str] = {}
        self._names: list[str] = []

    def load_vocab(self) -> int:
        """从 stock_info 加载 code→name 词表。

        重名处理 (#56 卡A): 同板族同名多码 (北交所换码对) 折叠到 klines
        终值较新者 (活码); 跨板同名对/无 klines 方向证据 → 整名剔除
        (歧义保护, 宁漏勿错)。
        """
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT code, name FROM stock_info WHERE name IS NOT NULL AND name != ''"
        ).fetchall()
        seen: dict[str, str] = {}
        dupes: dict[str, set[str]] = {}
        for r in rows:
            name = r["name"]
            if name in seen and seen[name] != r["code"]:
                dupes.setdefault(name, set()).update({seen[name], r["code"]})
            else:
                seen[name] = r["code"]
        folded = 0
        for name, codes in dupes.items():
            codes = {seen.get(name), *codes} - {None}
            codes.discard(None)
            codes = {c for c in codes if c}
            # 跨板对排除: 全体码两两同板才折叠 (真歧义不折)
            board_codes = sorted(codes)
            if len(board_codes) >= 2 and all(
                _same_board(board_codes[0], c) for c in board_codes[1:]
            ):
                winner = self._fold_direction(conn, codes)
                if winner:
                    seen[name] = winner
                    folded += 1
                    continue
            seen.pop(name, None)  # 跨板/无证据 → 整名剔除
        # 冒号路线用全词表 (公司新闻惯例, 常见名词也可精确配);
        # 子串路线剔除常见名词陷阱 (泛用名词防错配)
        self._name2code = seen
        self._names = sorted(
            (n for n in self._name2code if n not in COMMON_NOUN_BLOCKLIST),
            key=len, reverse=True,
        )
        logger.info(
            f"Attribution vocab: {len(self._name2code)} names "
            f"({len(dupes)} 重名组, {folded} 双码折叠, "
            f"{len(COMMON_NOUN_BLOCKLIST)} 常见名词禁子串)"
        )
        return len(self._name2code)

    @staticmethod
    def _fold_direction(conn, codes: set[str]) -> str | None:
        """同名多码 (同板族) → klines 终值较新者 = 活码。

        生产实证 (09-01 ro): 246 对 920xxx 活到 08-31 / 旧码统一死在
        07-17 (北交所 07-18 换码边界)。并列或皆无 kline → None (无方向
        证据, 宁漏勿错)。表不存在 (memory 测试库) → None。
        """
        latest: dict[str, str] = {}
        try:
            for c in sorted(codes):
                row = conn.execute(
                    "SELECT MAX(date) AS d FROM daily_klines WHERE code = ?", (c,)
                ).fetchone()
                d = row["d"] if row is not None else None
                if d:
                    latest[c] = d
        except Exception:
            return None
        if not latest:
            return None
        best = max(latest.values())
        winners = sorted(c for c, d in latest.items() if d == best)
        return winners[0] if len(winners) == 1 else None

    def _match_codes(self, text: str) -> list[str]:
        """文本中出现的全名 → 去重 code 列表 (长名优先, 命中后不重复计子串)"""
        if not text:
            return []
        codes: list[str] = []
        remaining = text
        for name in self._names:
            if name in remaining:
                codes.append(self._name2code[name])
                remaining = remaining.replace(name, "")
        # 去重保序
        seen: set[str] = set()
        uniq = []
        for c in codes:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq

    def _exit_guard(
        self, code: str, method: str, title: str, name: str
    ) -> Attribution | None:
        """G1 主语不变量 (#56 卡A): 命中名必须出现在 title, 违例 → None。

        统一出口校验, 防未来路线回归 (设计稿 §3.1): 有它, 已砍的
        vocab_summary 一条都进不来 — 名不在 title 的归属在这里死掉。
        """
        if not name or name not in (title or ""):
            return None
        return Attribution(code=code, method=method)

    def attribute(self, title: str, summary: str = "") -> Attribution | None:
        """归属单条新闻; 未命中或歧义 → None (宁漏勿错)"""
        # 路线1: 冒号格式前缀精确匹配 (东财个股新闻标准格式)
        if title:
            for sep in ("：", ":"):
                if sep in title:
                    prefix = title.split(sep, 1)[0].strip()
                    code = self._name2code.get(prefix)
                    if code:
                        return self._exit_guard(code, "vocab_colon", title, prefix)
                    break  # 只认第一个冒号分隔
        # G2 嵌套名防伪 (#56 卡A): title 含词表外更长实体 (负向别名表)
        # → 拒归属 (「宁可过滤掉真报不放走假报」)。冒号路线优先级更高,
        # 但前缀已未命中词表 → 此处长实体语境照拦。
        if title:
            for alias in NEGATIVE_ALIASES:
                if alias in title:
                    return None
        # 路线2: title 全名子串 (G3: 多命中 → None 宁漏勿错)
        codes = self._match_codes(title)
        if len(codes) == 1:
            name = next(
                (n for n, c in self._name2code.items() if c == codes[0] and n in title),
                None,
            )
            if name is not None:
                return self._exit_guard(codes[0], "vocab_title", title, name)
        # 路线3 (vocab_summary, summary 全名子串兜底) 已砍 (#56):
        # title 主语缺失时 summary 单命中 = 顺带提及错归属, 生产实测
        # 353/353 全误报 (0 真归属损失), 宁漏勿错 → 不归属。
        # 详见 vault 分层讨论/卡56-summary路线误报摸底报告-F-20260831.md
        return None


def audit_attribution(
    db: Database,
    date: str,
    *,
    enforce: bool = False,
    vocab_baseline: int | None = None,
) -> dict | None:
    """#56 卡B: 归属口径哨兵 (report-only) — B 复验 A 的规则。

    对当日已落库归属行按卡A三闸形状复验:
      G1 — 归属行的公司名 (code 反查词表) 不在 title → 违例
           (code 反查不到名 = 词表无此 code, 跳过不猜 — 折叠前存量旧码行
            属此形状, 哨兵不误报)
      G2 — title 含负向别名表条目却有归属 → 违例
      G3 — title 命中 ≥2 词表码却有归属 (取第一个型) → 违例

    口径三件 (metadata 落点, decay_alerts 同型):
      attribution_dist — 当日行按 method 分组 (空码计 none)
      vocab_drift — 词表 size + 日环比 delta (baseline=None → 首采锚定;
                    生产基线 09-01 实数 5754 = 5507 singles + 247 折叠,
                    含当日新股生长项, 新上市=预期漂移源非异常)
      violations — G1/G2/G3 计数

    只读 (SELECT-only, 零写); 库级默认关 (enforce=False → None,
    哨兵宪法); 段内异常由调用方 (NewsPipeline.run 哨兵段) try 包裹。
    """
    if not enforce:
        return None
    a = NewsAttributor(db)
    size = a.load_vocab()
    code2name = {c: n for n, c in a._name2code.items()}
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT title, code, attribution_method FROM stock_news "
        "WHERE date = ? AND code IS NOT NULL AND code != ''",
        (date,),
    ).fetchall()
    dist: dict[str, int] = {}
    violations = {"G1": 0, "G2": 0, "G3": 0}
    for r in rows:
        method = (r["attribution_method"] or "none").strip() or "none"
        dist[method] = dist.get(method, 0) + 1
        title = r["title"] or ""
        code = r["code"]
        name = code2name.get(code)
        if name is not None and name not in title:
            violations["G1"] += 1
        if any(alias in title for alias in NEGATIVE_ALIASES):
            violations["G2"] += 1
        if len(a._match_codes(title)) > 1:
            violations["G3"] += 1
    unattr = conn.execute(
        "SELECT COUNT(*) AS n FROM stock_news WHERE date = ? "
        "AND (code IS NULL OR code = '')",
        (date,),
    ).fetchone()
    none_n = (unattr["n"] or 0) if unattr else 0
    dist["none"] = dist.get("none", 0) + none_n
    drift = {
        "size": size,
        "baseline": vocab_baseline,
        "delta": (size - vocab_baseline) if vocab_baseline is not None else None,
    }
    return {
        "attribution_dist": dist,
        "vocab_drift": drift,
        "violations": violations,
    }

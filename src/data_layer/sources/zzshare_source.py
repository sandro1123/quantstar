"""zzshare 数据源适配器 — 免费 A 股数据源（龙虎榜/概念/情绪/涨停）"""
import time
from typing import Any

from common.logging import get_logger

logger = get_logger("data.zzshare")

# DL-49: 概念/题材板块 plate_type 语义 (zzshare plates 接口)
PLATE_TYPE_INDUSTRY = 14   # 行业 — concept_plates 不采 (行业走 industry_fallback 留痕)
PLATE_TYPE_CONCEPT = 15    # 概念
PLATE_TYPE_THEME = 17      # 题材
CONCEPT_PLATE_TYPES = (PLATE_TYPE_CONCEPT, PLATE_TYPE_THEME)

# source 列三态留痕 (DL-49 统筹前置②: plate_type 必须分开, 否则 concept_count
# 会再把行业算进去 — 这正是 concept_daily 语义事故的病根)
SOURCE_LABEL_BY_PLATE_TYPE = {
    PLATE_TYPE_CONCEPT: "zzshare_concept_15",
    PLATE_TYPE_THEME: "zzshare_theme_17",
}

# 回补数据打标后缀 (统筹裁定: 回补段有幸存者偏差 — plates_list 只给当前板块
# 目录, 历史被删板块不出现。回补数据与上线后自然积累数据必须可区分:
# IC 分两段算, 定权重只用无偏段)。
BACKFILL_SOURCE_SUFFIX = "_backfill"


class ZZShareSourceAdapter:
    name = "zzshare"
    capabilities = {"lhb", "limit_up", "sentiment", "stock_basic", "concept_plates",
                    "concept_intervals"}

    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            from zzshare.client import DataApi
            self._client = DataApi()
        return self._client

    def is_available(self) -> bool:
        try:
            import zzshare  # noqa: F401
            return True
        except ImportError:
            return False

    def get_data(self, data_type: str, params: dict) -> Any:
        client = self._get_client()
        if data_type == "lhb":
            return client.lhb_list(date1=params.get("start_date", ""), date2=params.get("end_date", ""))
        elif data_type == "limit_up":
            return client.review_uplimit_reason(date=params.get("date", ""))
        elif data_type == "stock_basic":
            df = client.stock_basic()
            return df.to_dict("records") if hasattr(df, 'to_dict') else (df if df else [])
        elif data_type == "sentiment":
            return client.updown_distribution(date1=params.get("date", ""))
        elif data_type == "concept_plates":
            return self._get_concept_plates(params)
        elif data_type == "concept_intervals":
            return self._get_concept_intervals(params)
        return None

    # ------------------------------------------------------------------
    # DL-49: 概念/题材板块 (plates_list + plates_stocks + plates_rank)
    # ------------------------------------------------------------------

    def get_plates_list(self, plate_type: int) -> list[dict]:
        """板块目录 — [{plate_code, plate_name, plate_type, date_in, ...}]

        ⚠️ 无 date 参数, 返回**当前**板块目录。历史回补用它逐板块查历史成分时,
        "板块宇宙"是前视的 (幸存者偏差): 历史上被删的板块不出现。
        故回补数据必须打 BACKFILL_SOURCE_SUFFIX 标, IC 分段处理 (统筹裁定)。
        """
        data = self._get_client().plates_list(plate_type=plate_type)
        return data if isinstance(data, list) else []

    def get_plates_stocks(self, plate_type: int, plate_code: str, date: str) -> list[dict]:
        """板块成分 — [{stock_code, stock_name, time_in, time_out, ...}]

        date 为时点过滤参数: 上游按 time_in<=date 返回, time_out 标识移出。
        实测 (2026-08-23): 历史成分可回溯到至少 2020-01-02 (成分时点判定 PIT 正确)。
        """
        data = self._get_client().plates_stocks(
            plate_type=plate_type, plate_code=plate_code, date=date)
        return data if isinstance(data, list) else []

    def get_plates_rank(self, plate_type: int, date: str, limit: int = 1000) -> list[dict]:
        """板块热度排名 — [{plate_code, plate_name, rate(当日涨跌幅%), ...}]

        rate 是 concept_daily.change_pct 的真源 (DL-49 前置③)。
        实测 (2026-08-23): date1 支持历史日期。
        """
        data = self._get_client().plates_rank(
            plate_type=plate_type, date1=date, limit=limit)
        return data if isinstance(data, list) else []

    def _get_concept_plates(self, params: dict) -> dict:
        """概念/题材板块全量拉取 → 归一化记录 (DL-49)。

        返回 {"records": [{code, date, concept_name, plate_code, plate_type,
                          source, change_pct}],
              "stats": {plates_total, plates_ok, plates_empty, rank_missing,
                        errors: [...]}}

        - plate_type 15/17 分开拉取并留痕 (source 列三态, 前置②);
        - 时点判定: time_in<=date 且 (time_out 空 或 time_out>date) = 当日在板;
        - change_pct 来自 plates_rank.rate 映射, **映射没有的写 None 不写 0.0**
          (前置③, F-45 教训: 编造的 0.0 → 常量 → z-score 恒 0 → 因子静默死亡);
        - 空板块跳过并计数 (统筹附加③, 暴增即源侧异常信号);
        - 温和限速 sleep (实测 1018 板块 0.05s 间隔 133.5s 零错误);
        - params["backfill"]=True 时 source 加 _backfill 后缀 (幸存者偏差打标,
          统筹裁定: 回补段 IC 只作方向参考, 不进权重公式)。
        """
        date = params.get("date", "")
        sleep_s = float(params.get("sleep_s", 0.05))
        backfill = bool(params.get("backfill", False))
        records: list[dict] = []
        stats = {"plates_total": 0, "plates_ok": 0, "plates_empty": 0,
                 "rank_missing": 0, "errors": []}

        for pt in CONCEPT_PLATE_TYPES:
            # 板块涨跌幅映射: plate_code -> rate
            rate_map: dict[str, float] = {}
            try:
                for r in self.get_plates_rank(pt, date):
                    pc = r.get("plate_code", "")
                    rate = r.get("rate")
                    if pc and rate is not None:
                        try:
                            rate_map[pc] = float(rate)
                        except (TypeError, ValueError):
                            pass
            except Exception as e:
                stats["errors"].append(f"plates_rank({pt}): {type(e).__name__}: {str(e)[:100]}")
                logger.warning(f"zzshare plates_rank({pt}) failed, change_pct 将全为 NULL: {e}")

            try:
                if backfill:
                    # 无偏回补 (统筹 sm-0e53a77503a841fe GO): 宇宙改用 plates_rank(date)
                    # —— 当日活跃板块, PIT 正确且含已删板块 (实证: 2021/2022 各 2 只
                    # 已删板块出现在历史 rank 但不在当前 plates_list 目录)。
                    # plates_list 只给当前目录 → 前视宇宙 → 幸存者偏差, 回补弃用。
                    # plates_rank 返回 plate_code+plate_name, 与 plates_list 同形。
                    plates = self.get_plates_rank(pt, date)
                else:
                    plates = self.get_plates_list(pt)
            except Exception as e:
                stats["errors"].append(f"plates_universe({pt}): {type(e).__name__}: {str(e)[:100]}")
                logger.warning(f"zzshare plates_universe({pt}) failed: {e}")
                continue
            stats["plates_total"] += len(plates)

            source_label = SOURCE_LABEL_BY_PLATE_TYPE[pt]
            if backfill:
                source_label += BACKFILL_SOURCE_SUFFIX
            for p in plates:
                pc = p.get("plate_code", "")
                pn = p.get("plate_name", "")
                if not pc or not pn:
                    continue
                try:
                    stocks = self.get_plates_stocks(pt, pc, date)
                except Exception as e:
                    stats["errors"].append(f"plates_stocks({pt},{pc}): {type(e).__name__}: {str(e)[:80]}")
                    continue
                # 时点在板判定 (PIT 正确: time_in/time_out 是上游给的进出板日期)
                active = [
                    s for s in stocks
                    if s.get("stock_code")
                    and (s.get("time_in") or "") <= date
                    and (not s.get("time_out") or s.get("time_out") > date)
                ]
                if not active:
                    stats["plates_empty"] += 1
                    continue
                stats["plates_ok"] += 1
                change_pct = rate_map.get(pc)
                if change_pct is None:
                    stats["rank_missing"] += 1
                for s in active:
                    records.append({
                        "code": str(s["stock_code"]),
                        "date": date,
                        "concept_name": pn,
                        "plate_code": pc,
                        "plate_type": pt,
                        "source": source_label,
                        "change_pct": change_pct,  # None = 未知, 禁止写 0.0
                    })
                time.sleep(sleep_s)

        return {"records": records, "stats": stats}

    def _get_concept_intervals(self, params: dict) -> dict:
        """概念/题材板块**区间**拉取 → 成员区间 + 板块日涨幅 (区间存储, 统筹 sm-337ab3642f7f486f 批准)。

        与 _get_concept_plates (快照: 当日在板展开) 的区别: 这里保留上游给的
        time_in/time_out 原始区间, 不做当日 PIT 过滤 —— 一次拉取即得历史进出板
        全貌, 落 concept_membership(code, concept_name, plate_code, plate_type,
        source, time_in, time_out) + plate_daily_rate(plate_code, date, rate)。

        返回 {"memberships": [{code, concept_name, plate_code, plate_type,
                              source, time_in, time_out}],
              "rates": [{plate_code, plate_type, date, rate, source}],
              "stats": {plates_total, plates_ok, plates_empty, rank_missing,
                        memberships, errors: [...]}}

        约束 (统筹五约束):
        - ① plate_type 三态留痕: source 列按 15/17 分开打标;
        - ② rate 拿不到写 NULL, 禁止 0.0 (F-45 教训); 仍落一行留"当日在榜"痕迹;
        - ④ 空板块 (无任何 time_in<=date 成员) 跳过并计数;
        - params["backfill"]=True → source 加 _backfill 后缀 (plates_list 只给
          当前板块目录, 幸存者偏差打标, D-104 待决, IC 分段)。

        上游语义: plates_stocks(date=X) 返回 time_in<=X 的全部成员 (含已出板,
        time_out 有值) —— 故每日增量跑一次即可累积区间; 出板事件表现为旧行
        time_out 从 NULL 变有值, 写入侧按 (code, plate_code, time_in) 幂等覆盖。
        """
        date = params.get("date", "")
        sleep_s = float(params.get("sleep_s", 0.05))
        backfill = bool(params.get("backfill", False))
        memberships: list[dict] = []
        rates: list[dict] = []
        stats = {"plates_total": 0, "plates_ok": 0, "plates_empty": 0,
                 "rank_missing": 0, "memberships": 0, "errors": []}

        for pt in CONCEPT_PLATE_TYPES:
            # 板块当日涨幅 → plate_daily_rate (rate 拿不到不写该行, 禁止 0.0)
            try:
                rank_rows = self.get_plates_rank(pt, date)
            except Exception as e:
                rank_rows = []
                stats["errors"].append(f"plates_rank({pt}): {type(e).__name__}: {str(e)[:100]}")
                logger.warning(f"zzshare plates_rank({pt}) failed, 当日 rate 缺失: {e}")
            source_label = SOURCE_LABEL_BY_PLATE_TYPE[pt]
            if backfill:
                source_label += BACKFILL_SOURCE_SUFFIX
            seen_plates: set[str] = set()
            for r in rank_rows:
                pc = r.get("plate_code", "")
                rate = r.get("rate")
                if not pc or pc in seen_plates:
                    continue
                seen_plates.add(pc)
                rate_val = None
                if rate is not None:
                    try:
                        rate_val = float(rate)
                    except (TypeError, ValueError):
                        rate_val = None
                if rate_val is None:
                    stats["rank_missing"] += 1
                # ② 拿不到写 NULL, 禁止 0.0 — 仍落一行, 留"该板块当日在榜"痕迹
                # (区别于"根本没进榜"=无行; 铁律 #12 可观测)。
                rates.append({
                    "plate_code": pc, "plate_type": pt, "date": date,
                    "rate": rate_val, "source": source_label,
                })

            try:
                plates = self.get_plates_list(pt)
            except Exception as e:
                stats["errors"].append(f"plates_list({pt}): {type(e).__name__}: {str(e)[:100]}")
                logger.warning(f"zzshare plates_list({pt}) failed: {e}")
                continue
            stats["plates_total"] += len(plates)

            for p in plates:
                pc = p.get("plate_code", "")
                pn = p.get("plate_name", "")
                if not pc or not pn:
                    continue
                try:
                    stocks = self.get_plates_stocks(pt, pc, date)
                except Exception as e:
                    stats["errors"].append(f"plates_stocks({pt},{pc}): {type(e).__name__}: {str(e)[:80]}")
                    continue
                # 区间保留: time_in<=date 的全部成员 (含已出板), 不做当日 PIT 过滤。
                # time_in 缺失的行无法构成区间 (PK 含 time_in), 跳过并计数。
                rows = [s for s in stocks
                        if s.get("stock_code") and s.get("time_in")
                        and s.get("time_in") <= date]
                if len(rows) < len([s for s in stocks if s.get("stock_code")]):
                    stats["time_in_missing"] = stats.get("time_in_missing", 0) + (
                        len([s for s in stocks if s.get("stock_code")]) - len(rows))
                if not rows:
                    stats["plates_empty"] += 1  # ④ 空板块跳过并计数
                    continue
                stats["plates_ok"] += 1
                for s in rows:
                    memberships.append({
                        "code": str(s["stock_code"]),
                        "concept_name": pn,
                        "plate_code": pc,
                        "plate_type": pt,
                        "source": source_label,
                        "time_in": s.get("time_in") or None,
                        "time_out": s.get("time_out") or None,  # 空 → NULL (仍在板)
                    })
                time.sleep(sleep_s)

        stats["memberships"] = len(memberships)
        return {"memberships": memberships, "rates": rates, "stats": stats}

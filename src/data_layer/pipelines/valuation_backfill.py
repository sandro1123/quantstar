# -*- coding: utf-8 -*-
"""data_layer/pipelines/valuation_backfill.py — 估值顶层补跑腿 (丙件, 2026-09-25)。

病 (09-16~09-24 生产实证): tushare bulk 限流 → 当日 kline 只写 ~1/3 →
valuation 主跑采到 1908/5553 (34%) 判 failed, **当日缺口 = 终态** ——
T+1 不补 (实证: 09-17 的 1785 行到 09-18 仍是 1785)。根因是没有同族补跑腿:
DL-51 给 kline 配了 17:40 兜底 (baostock), valuation 漏配。

**为什么不复用主跑**: 主跑走 bulk 源 + expected 分母门禁; 断供日是**源端限流**
(bulk 拿不满), 再跑一遍仍是 34% → 必须换**逐股兜底源**。

**兜底源 = baostock** (统筹钉, 三理由):
  ① 免 token 无 tushare 式限流, 17:30 后入库稳定 (DL-51 kline 兜底已实证)
  ② 复用 DL-51「17:30 入库 → 17:40 兜底」同族时序窗
  ③ akshare 东财不可依赖 (stock_value_em 39.79s 对 3600 缺口零贡献 + 反爬)
实测 (生产适配器): 0.137s/只, p90=0.291s, 95% 命中, 外推 3600 只串行 **8.2 分钟**。

**部分字段接受** (统筹裁): baostock 只给 pe_ttm/pb_mrq/ps_ttm;
market_cap/total_shares/dividend_yield 返回 None → **必须合并写**
(upsert_preserving), 不得让 None 覆盖主跑已写的好值 (判例「宁缺勿脏」)。
下游 size 因子有 is_valid_positive 守卫接着, **缺不是脏**。

**幂等短路**: 缺口低于 GAP_SHORT_CIRCUIT_RATIO 则零开销返回
(主跑成功日不重复跑, 与 DL-51 kline 同款语义)。
"""
from __future__ import annotations

from common.logging import get_logger
from data_layer.pipelines.base import (
    BasePipeline, PipelineInput, PipelineResult,
)
from data_layer.repos import get_repo

logger = get_logger("data.valuation_backfill")

# 幂等短路阈值 (单一定义点): 缺口占当日有 kline 代码的比例低于此值则不补。
# 取 2% 的依据: 正常日的零星缺口 (新上市/停牌恢复/源端个别缺) 不值得跑 8 分钟
# 兜底; 断供日是 34%~66% 量级, 远超此阈, 必被补。
GAP_SHORT_CIRCUIT_RATIO = 0.02

# 单批最多补多少只 (防御性上界; 全市场 ~5553, 留足余量)
MAX_BACKFILL_CODES = 8000

# 三主字段 (全 None 的记录视为空壳, 拒写)
_PRIMARY_FIELDS = ("pe_ttm", "pb_mrq", "ps_ttm")


class ValuationBackfillPipeline(BasePipeline):
    """估值顶层补跑腿 — 主跑残缺时的逐股兜底。

    **不继承 ValuationPipeline**: 主跑带 expected 分母门禁 + bulk 源路径,
    继承会把「断供日必失败」那个行为一并带上 (丙件要的正是绕过它)。
    """

    name = "valuation_backfill"
    requires = ["daily_klines"]
    produces = ["stock_valuation"]
    capability = "valuation"

    def __init__(self, source_registry=None):
        # 与 ValuationPipeline 同款注入缝 (测试可传假注册表)
        self.source_registry = source_registry

    def run(self, input: PipelineInput) -> PipelineResult:
        repo = get_repo(input.db)
        date = input.date
        db = input.db

        # ---- 缺口面: 当日有 kline 但 valuation 无行 ----
        try:
            codes_with_kline = repo.kline.get_codes_by_date(date)
        except Exception as exc:  # noqa: BLE001
            return PipelineResult(
                name=self.name, date=date, status="failed",
                errors=[f"读取当日 kline 代码失败: {exc}"],
            )
        if not codes_with_kline:
            return PipelineResult(
                name=self.name, date=date, status="failed",
                errors=["当日无 kline 代码 — 补跑无口径 (kline 腿未落库?)"],
            )

        existing = self._existing_codes(db, date)
        gap = [c for c in codes_with_kline if c not in existing]
        total = len(codes_with_kline)
        gap_ratio = len(gap) / total if total else 0.0

        # ---- 幂等短路 ----
        if gap_ratio < GAP_SHORT_CIRCUIT_RATIO:
            logger.info(
                f"valuation_backfill 短路: 缺口 {len(gap)}/{total} "
                f"({gap_ratio:.2%}) < {GAP_SHORT_CIRCUIT_RATIO:.2%}, 零开销返回")
            return PipelineResult(
                name=self.name, date=date, status="success",
                records_read=0, records_written=0,
                metadata={"short_circuited": True, "gap": len(gap),
                          "total": total, "gap_ratio": round(gap_ratio, 6)},
            )

        if len(gap) > MAX_BACKFILL_CODES:
            gap = gap[:MAX_BACKFILL_CODES]

        # ---- 逐股兜底源补 ----
        records, warnings, failures = self._fetch_gap(input, gap, date)

        if not records:
            return PipelineResult(
                name=self.name, date=date, status="failed",
                records_read=len(gap), records_written=0,
                errors=(failures[:20] or
                        [f"兜底源未补到任何记录 (缺口 {len(gap)} 只)"]),
                warnings=warnings,
                metadata={"short_circuited": False, "gap": len(gap),
                          "total": total, "gap_ratio": round(gap_ratio, 6)},
            )

        written = 0
        if not input.dry_run:
            written = self._write_preserving(repo, records)

        logger.info(
            f"valuation_backfill: 缺口 {len(gap)} 只 → 补写 {written} 行 "
            f"(缺口率 {gap_ratio:.1%})")
        return PipelineResult(
            name=self.name, date=date,
            status="success" if not failures else "partial",
            records_read=len(gap), records_written=written,
            warnings=warnings, errors=failures[:20],
            metadata={"short_circuited": False, "gap": len(gap),
                      "total": total, "gap_ratio": round(gap_ratio, 6),
                      "source": "baostock"},
        )

    # ---- 内部 ----

    def _existing_codes(self, db, date: str) -> set[str]:
        try:
            conn = db.get_connection()
            rows = conn.execute(
                "SELECT code FROM stock_valuation WHERE date=?", (date,)
            ).fetchall()
            return {r[0] for r in rows}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取当日已有估值失败 (按零已存处理): {exc}")
            return set()

    def _fetch_gap(self, input: PipelineInput, gap: list[str],
                   date: str) -> tuple[list[dict], list[str], list[str]]:
        """逐只走降级链; 单只失败留痕不中断整批。"""
        records: list[dict] = []
        warnings: list[str] = []
        failures: list[str] = []
        sources = self.source_registry.get_sources_for(self.capability)

        for code in gap:
            got = None
            for source in sources:
                try:
                    raw = source.get_data(self.capability, {
                        "code": code, "date": date,
                        # baostock 的 get_data("valuation") 读 start_date/end_date
                        # (不是 date); 只传 date 会静默返回空 → 补跑腿空转。
                        # 两条都传, 兼容两种签名 (单调用源读 date, 区间源读 start/end)。
                        "start_date": date, "end_date": date,
                    })
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{source.name}/{code}: {exc}")
                    continue
                if not raw:
                    continue
                # 区间源返回 list, 取当日那条; 单值源返回 dict 直接用
                if isinstance(raw, list):
                    raw = next(
                        (r for r in raw
                         if str(r.get("date", "")).replace("-", "")
                         == str(date).replace("-", "")), None)
                    if raw is None:
                        continue
                rec = self._normalize(raw, code, date)
                if rec is None:
                    continue
                got = rec
                break
            if got is None:
                warnings.append(f"{code}: 兜底源均无数据")
                continue
            records.append(got)
        return records, warnings, failures

    @staticmethod
    def _normalize(raw: dict, code: str, date: str) -> dict | None:
        """规范化 + 空壳拒绝: 三主字段全 None 视为无效记录。"""
        if not isinstance(raw, dict):
            return None
        rec = {
            "code": code,
            "date": date,
            "pe_ttm": raw.get("pe_ttm"),
            "pb_mrq": raw.get("pb_mrq"),
            "ps_ttm": raw.get("ps_ttm"),
            "market_cap": raw.get("market_cap"),
            "total_shares": raw.get("total_shares"),
            "dividend_yield": raw.get("dividend_yield"),
        }
        if not any(rec[f] is not None and rec[f] != 0.0
                   for f in _PRIMARY_FIELDS):
            return None
        return rec

    def _write_preserving(self, repo, records: list[dict]) -> int:
        """合并写: 只覆盖非 None 字段 (None 不覆盖已存好值)。

        判例「宁缺勿脏」: baostock 的 market_cap=None 是真"没有该数据",
        若用全字段 upsert 覆盖, 会把主跑已写好的市值洗成 NULL = 把好数据弄脏。
        """
        writer = getattr(repo.valuation, "upsert_preserving", None)
        if callable(writer):
            writer(records)
            return len(records)
        # repo 无 preserving 写 → 逐字段 UPDATE 只写非 None (等价语义)
        conn = repo.valuation.db.get_connection()
        n = 0
        for rec in records:
            cols = {k: v for k, v in rec.items()
                    if k not in ("code", "date") and v is not None}
            if not cols:
                continue
            sets = ", ".join(f"{k}=?" for k in cols)
            cur = conn.execute(
                f"UPDATE stock_valuation SET {sets} WHERE code=? AND date=?",
                (*cols.values(), rec["code"], rec["date"]))
            if cur.rowcount == 0:
                keys = ", ".join(cols)
                marks = ", ".join("?" for _ in cols)
                conn.execute(
                    f"INSERT OR IGNORE INTO stock_valuation "
                    f"(code, date, {keys}) VALUES (?, ?, {marks})",
                    (rec["code"], rec["date"], *cols.values()))
            n += 1
        conn.commit()
        return n

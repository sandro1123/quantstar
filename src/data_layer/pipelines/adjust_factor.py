"""pipelines/adjust_factor.py — DL-35 复权因子/分红/送转 每日增量采集

设计判断 (回执说明):
1. **接线位置=独立管道, 不并入 daily_data_pipeline**。理由:
   ① baostock 复权因子入库时间 18:00, 主采集 15:30/16:00 跑时当日数据还不存在;
   ② 主管道已因慢源超时出过事故 (2400s), 不再叠加外部源;
   ③ 复权因子属"可回补"数据 (盘点文档 §九), 漏一天不丢失——幂等
      INSERT OR REPLACE, 次日运行自动补齐前日。
   crontab 行 (18:30) 需 GO, 只交付文本 (铁律 #11)。
2. **交叉验证不用 FusionStrategy.fuse() 逐值对比**。理由: 复权因子各源基准不同
   (tushare 累计因子 8.6463 vs baostock 事件因子 7.669, 基准不同不可直比),
   逐值对比会恒误报。真正可比的是**每股现金分红**与**送转合计因子**:
   baostock dividCashPsBeforeTax(元/股) vs mootdx fenhong(元/10股, ÷10),
   baostock 1+送+转 vs mootdx 1+songzhuangu/10。实测茅台 28.02423 vs 280.24/10
   一致。故在管道内做定向对比, 差异记 warnings 不阻塞 (D198 模式)。
3. **splits 送转解析**: baostock 结构化数值字段为主 (dividStocksPs 送股/股、
   dividReserveToStockPs 转增/股, 已是每股口径无需解析文本); mootdx xdxr
   songzhuangu (每10股) 做交叉验证。不解析 baostock dividCashStock 文本字段。

口径 (D-40): adjust_factors.adjust_factor = 后复权因子 (baostock backAdjustFactor),
向前稳定。date = 除权除息日。
"""

from __future__ import annotations

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.repos.adjust import AdjustRepo

logger = get_logger("data.adjust_factor")

# 交叉验证容差 (相对)
_XVAL_TOL = 0.02


class AdjustFactorPipeline(BasePipeline):
    name = "adjust_factor"
    requires: list[str] = []
    produces = ["adjust_factors", "dividends", "splits"]

    def __init__(self, source_registry=None):
        self.source_registry = source_registry

    def _get_source(self, name: str):
        if not self.source_registry:
            return None
        for s in self.source_registry.get_all_sources():
            if s.name == name and s.is_available():
                return s
        return None

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db:
            from common.database import Database
            input.db = Database()
        repo = AdjustRepo(input.db)
        date = input.date
        warnings: list[str] = []
        errors: list[str] = []

        bs = self._get_source("baostock")
        if bs is None:
            return PipelineResult(
                name=self.name, date=date, status="failed",
                errors=["baostock 不可用 (复权因子主源)"],
                metadata={"mode": "daily_increment"})

        # 1. 当日全市场复权因子增量 (baostock query_daily_adjust_factor, ~15 行/日)
        try:
            events = bs.get_daily_adjust_factors(date)
        except Exception as e:  # noqa: BLE001
            return PipelineResult(
                name=self.name, date=date, status="failed",
                errors=[f"baostock daily_adjust_factor 失败: {e}"],
                metadata={"mode": "daily_increment"})

        factor_records = [
            {"code": ev["code"], "ex_date": ev["ex_date"],
             "adjust_factor": ev["back_factor"], "source": "baostock"}
            for ev in events if ev.get("back_factor")]
        n_factors = repo.upsert_adjust_factors(factor_records)

        # 2. 当日有除权事件的股票 → 分红/送转明细 (baostock 逐只, ~15 只/日)
        year = int(date[:4]) if len(date) >= 4 else 0
        div_records: list[dict] = []
        split_records: list[dict] = []
        for ev in events:
            code = ev["code"]
            try:
                divs = bs.get_dividends(code, year)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{code} 分红明细拉取失败: {e}")
                continue
            for d in divs:
                if d["ex_date"] != date:
                    continue  # 只要当日除权的事件
                div_records.append(d)
                sd = d.get("stock_dividend", 0.0) or 0.0
                st = d.get("stock_transfer", 0.0) or 0.0
                if sd + st > 0:
                    split_records.append({
                        "code": code, "ex_date": date,
                        "split_factor": 1.0 + sd + st, "source": "baostock"})
        n_divs = repo.upsert_dividends(div_records)
        n_splits = repo.upsert_splits(split_records)

        # 3. 交叉验证 (baostock vs mootdx, 定向对比, 差异记 warnings 不阻塞)
        if div_records:
            warnings.extend(self._cross_validate(div_records))

        total = n_factors + n_divs + n_splits
        if not events:
            # 当日无除权事件是正常情况 (~15 行/日, 很多交易日为 0)
            logger.info(f"adjust_factor {date}: 当日无除权事件")
        status = "failed" if errors and total == 0 else "success"
        return PipelineResult(
            name=self.name, date=date, status=status,
            records_written=total, errors=errors, warnings=warnings,
            metadata={"mode": "daily_increment", "events": len(events),
                      "factors": n_factors, "dividends": n_divs,
                      "splits": n_splits})

    def _cross_validate(self, div_records: list[dict]) -> list[str]:
        """baostock vs mootdx xdxr 定向对比: 每股现金分红 + 送转合计因子。

        只对比两源都给出非零值的字段; mootdx 不可用则静默跳过 (不阻塞)。
        """
        warnings: list[str] = []
        mootdx = self._get_source("mootdx")
        if mootdx is None or not hasattr(mootdx, "get_xdxr"):
            return warnings
        for d in div_records[:10]:  # 抽样, 控制调用量
            code, ex_date = d["code"], d["ex_date"]
            try:
                xd = mootdx.get_xdxr(code, ex_date)
            except Exception:  # noqa: BLE001
                continue
            if xd is None:
                continue
            # 现金分红: baostock 元/股 vs mootdx fenhong 元/10股
            bs_cash = d.get("cash_dividend", 0.0) or 0.0
            md_cash = (xd.get("fenhong", 0.0) or 0.0) / 10.0
            if bs_cash > 0 and md_cash > 0 and abs(bs_cash - md_cash) / bs_cash > _XVAL_TOL:
                warnings.append(
                    f"{code} {ex_date} 现金分红不一致: baostock={bs_cash:.4f} "
                    f"mootdx={md_cash:.4f} (元/股)")
            # 送转合计: baostock 1+送+转 vs mootdx 1+songzhuangu/10
            bs_split = 1.0 + (d.get("stock_dividend", 0.0) or 0.0) + (d.get("stock_transfer", 0.0) or 0.0)
            md_split = 1.0 + (xd.get("songzhuangu", 0.0) or 0.0) / 10.0
            if bs_split > 1.0 and md_split > 1.0 and abs(bs_split - md_split) / bs_split > _XVAL_TOL:
                warnings.append(
                    f"{code} {ex_date} 送转因子不一致: baostock={bs_split:.4f} "
                    f"mootdx={md_split:.4f}")
        return warnings

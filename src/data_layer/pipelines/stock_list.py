"""股票代码清单管道 — 从 akshare_sina 全市场同步 code+name 到 stock_info

DL-XX: 名录 diff 退市检测 — 本次同步后, 对比 stock_info 中 is_active=1
但不在本次名录中的股票, 标记 is_active=0 + delist_date。
安全阈值: 缺失数 > max(50, 5% active) 时判定为源残缺, 跳过标记。
tushare delist 接口机会性补齐精确 delist_date (限流/失败不致命)。
"""
from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.repos.stock import StockRepo

logger = get_logger("data.stock_list")


class StockListPipeline(BasePipeline):
    name = "stock_list"
    requires: list[str] = []
    produces = ["stock_info"]

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db:
            from common.database import Database
            input.db = Database()
        errors = []
        total = 0

        try:
            from data_layer.sources.akshare_sina_source import AkshareSinaAdapter
            source = AkshareSinaAdapter()
            records = source.get_data("stock_list", {})
        except Exception as e:
            return PipelineResult(
                name=self.name, date=input.date,
                status="failed", errors=[f"stock_list 源: {e}"],
            )

        if not records:
            return PipelineResult(
                name=self.name, date=input.date,
                status="failed", errors=["stock_list 源返回空"],
            )

        # DL-21: tushare stock_basic 原生含 list_date/industry, 可用时
        # 机会性富化名录 (单次尝试, 限流/失败不致命, 回落纯名录)
        try:
            from data_layer.sources.tushare_source import TushareSourceAdapter
            ts_source = TushareSourceAdapter()
            if ts_source.is_available():
                ts_records = ts_source.get_data("stock_list", {}) or []
                if ts_records:
                    ts_map = {r["code"]: r for r in ts_records}
                    for rec in records:
                        hit = ts_map.get(rec["code"])
                        if hit:
                            if hit.get("list_date"):
                                rec["list_date"] = hit["list_date"]
                            if hit.get("industry") and not rec.get("industry"):
                                rec["industry"] = hit["industry"]
        except Exception as e:
            errors.append(f"tushare list_date 富化跳过: {e}")

        try:
            repo = StockRepo(input.db)
            repo.batch_upsert(records)
            total = len(records)
        except Exception as e:
            return PipelineResult(
                name=self.name, date=input.date,
                status="failed", errors=[f"stock_info 写入: {e}"],
            )

        # DL-XX: 名录 diff 退市检测
        delist_marked = self._detect_delist(input, records, errors)

        return PipelineResult(
            name=self.name, date=input.date,
            status="success", records_written=total,
            errors=errors,
            metadata={"delist_marked": delist_marked},
        )

    def _detect_delist(self, input: PipelineInput, records: list[dict],
                       errors: list[str]) -> int:
        """对比本次名录与 stock_info 活跃集, 标记消失的股票为退市。

        安全阈值: 缺失数 > max(50, 5% active) → 判定源残缺, 跳过。
        返回标记数量 (0 = 无标记或跳过)。
        """
        try:
            conn = input.db.get_connection()
            current_codes = {rec["code"] for rec in records}
            rows = conn.execute(
                "SELECT code FROM stock_info WHERE is_active = 1"
            ).fetchall()
            active_codes = {r[0] for r in rows}
            # D3 案 (2026-09-11): diff 判定只认沪深 0/3/6 头 — 北交所 8/4 旧码
            # 与 92 新码名录源覆盖不全, 计入会把真退市股顶翻安全闸
            # (本管线上线至 2026-09 零通电, 209 只死股挂 is_active=1 数月)。
            missing = {c for c in (active_codes - current_codes)
                       if c[:1] in "036"}

            if not missing:
                return 0

            # 安全阈值: 防源残缺导致误标大批退市
            threshold = max(50, int(len(active_codes) * 0.05))
            if len(missing) > threshold:
                logger.warning(
                    f"stock_list delist_diff: {len(missing)} missing > "
                    f"threshold {threshold} — 疑似源残缺, 跳过标记"
                )
                errors.append(
                    f"delist_diff_skipped: {len(missing)} missing > {threshold}"
                )
                return 0

            # 标记退市
            delist_date = input.date.replace("-", "")
            for code in sorted(missing):
                conn.execute(
                    "UPDATE stock_info SET is_active = 0, delist_date = ? "
                    "WHERE code = ?",
                    (delist_date, code),
                )
            conn.commit()
            logger.info(
                f"stock_list delist_diff: 标记 {len(missing)} 只退市 "
                f"(delist_date={delist_date}): {sorted(missing)[:10]}"
            )

            # 机会性 tushare delist_date 精确化
            self._refine_delist_date(conn, missing, errors)
            return len(missing)

        except Exception as e:
            errors.append(f"delist_diff failed: {e}")
            return 0

    def _refine_delist_date(self, conn, missing: set[str],
                            errors: list[str]) -> None:
        """用 tushare stock_basic(list_status=D) 补齐精确退市日期。"""
        try:
            from data_layer.sources.tushare_source import TushareSourceAdapter
            ts_source = TushareSourceAdapter()
            if not ts_source.is_available():
                return
            delist_records = ts_source.get_data("delist", {}) or []
            ts_map = {r["code"]: r.get("delist_date", "") for r in delist_records}
            refined = 0
            for code in missing:
                ts_date = ts_map.get(code, "")
                if ts_date:
                    conn.execute(
                        "UPDATE stock_info SET delist_date = ? WHERE code = ?",
                        (ts_date, code),
                    )
                    refined += 1
            if refined:
                conn.commit()
                logger.info(
                    f"stock_list: tushare 精确化 {refined} 条 delist_date"
                )
        except Exception as e:
            errors.append(f"tushare delist 精确化跳过: {e}")

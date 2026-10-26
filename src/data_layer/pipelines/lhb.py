"""龙虎榜数据管道 — 从 SourceRegistry 获取龙虎榜数据

DL-01 后优先走 FusionStrategy.fuse() (多源交叉验证, 差异告警不阻塞, D198);
无 fusion 时退回多源 fallback。

卡② (2026-09-15 lhb 全零事故, 09-16 取证): params 显式带 report_date —
源按**当日**取数, 禁缺省回退 T-1; 源契约不可得时腿报 failed 且 reason
指向病因 (统筹裁定 sm-13caf48691a54065 + 铁律 #12 降级不得静默)。
"""
from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.sources.zzshare_source import ZZShareSourceAdapter

logger = get_logger("data.lhb")


class LHBPipeline(BasePipeline):
    name = "lhb_daily"
    requires = []
    produces = ["lhb_daily"]

    def __init__(self, db=None, source_registry=None, fusion=None):
        self.db = db
        self.source_registry = source_registry
        self.fusion = fusion

    def _available_source_names(self) -> list[str]:
        """当前对 lhb 可用的源名 (用于失败信息点名病因)。"""
        reg = self.source_registry
        if reg is None and self.fusion is not None:
            reg = getattr(self.fusion, "registry", None)
        if reg is None:
            return []
        try:
            return [s.name for s in reg.get_sources_for("lhb")]
        except Exception:  # noqa: BLE001 — 诊断辅助不得反噬主流程
            return []

    def _no_data_reason(self, date: str, source_errors: list[str]) -> str:
        """构造指向病因的失败信息。

        统筹裁定: 「失败信息必须指向病因, 不是指向症状」——旧文案
        "所有源均无数据" 让运维去猜是哪个源; 现点名源与具体错因。
        """
        names = self._available_source_names()
        src_txt = "/".join(names) if names else "无可用源"
        detail = "; ".join(source_errors) if source_errors else "各源返回 0 行"
        return f"LHB: report_date={date} 不可得 (源: {src_txt}; {detail})"

    def _fetch(self, date: str):
        """返回 (rows, source_label, validation_warnings, fetch_error)

        fetch_error 非空 = **源契约不可得** (源抛错/不可用) → 腿 failed;
        fetch_error 为空且无行 = 源干净返回空 (可能尚未发布) → 腿 partial
        (保留既有 benign 语义, 不误伤"源还没发"的正常早跑场景)。
        """
        params = {"start_date": date, "end_date": date, "report_date": date}
        source_errors: list[str] = []

        if self.fusion is not None:
            try:
                rows, report = self.fusion.fuse("lhb", params, key_field="code")
            except Exception as e:  # noqa: BLE001 — 源契约错误须转 failed 而非冒泡
                source_errors.append(f"{type(e).__name__}: {e}")
                rows, report = None, None
            warnings = list(report.warnings) if report is not None else []
            if report is not None:
                se = getattr(report, "source_errors", None)
                if isinstance(se, dict):
                    for src, err in se.items():
                        source_errors.append(f"{src}: {err}")
                if report.has_warnings:
                    logger.warning(f"lhb 多源差异: {len(report.warnings)} 条告警")
            if rows:
                logger.info(f"lhb via fusion: {len(rows)} rows")
                return rows, "fusion", warnings, ""
            return None, "", warnings, (
                self._no_data_reason(date, source_errors) if source_errors else "")

        if not self.source_registry:
            try:
                return ZZShareSourceAdapter().get_data("lhb", params), "zzshare", [], ""
            except Exception as e:
                logger.debug(f"zzshare lhb failed: {e}")
                return None, "", [], self._no_data_reason(date, [f"zzshare: {e}"])
        for source in self.source_registry.get_sources_for("lhb"):
            try:
                rows = source.get_data("lhb", params)
                if rows:
                    logger.info(f"lhb from {source.name}: {len(rows)} rows")
                    return rows, source.name, [], ""
            except Exception as e:
                logger.debug(f"Source {source.name} failed for lhb: {e}")
                source_errors.append(f"{source.name}: {e}")
                continue
        return None, "", [], (
            self._no_data_reason(date, source_errors) if source_errors else "")

    def run(self, input: PipelineInput) -> PipelineResult:
        if not input.db and not self.db:
            from common.database import Database
            self.db = Database()
        db = input.db or self.db
        conn = db.get_connection()
        errors = []
        warnings = []
        total = 0

        conn.execute("""
            CREATE TABLE IF NOT EXISTS lhb_daily (
                code TEXT, date TEXT, net_buy_amount REAL DEFAULT 0,
                total_buy REAL DEFAULT 0, total_sell REAL DEFAULT 0,
                PRIMARY KEY (code, date)
            )
        """)

        rows, _src, fetch_warnings, fetch_error = self._fetch(input.date)
        warnings.extend(fetch_warnings)
        # 先校验整批再写入，避免陈旧源行占据请求日主键后被 OR IGNORE 锁住。
        # 无日期字段的源仍依赖显式 report_date 请求契约；不伪造源日期。
        for row in rows or []:
            for field in ("trade_date", "date", "report_date"):
                source_date = row.get(field)
                if source_date is not None and str(source_date) != input.date:
                    errors.append(
                        f"LHB: {field}={source_date} 与业务日 {input.date} 不符")
        if errors:
            return PipelineResult(name=self.name, date=input.date, status="failed",
                                  records_written=0, errors=errors, warnings=warnings)
        if rows:
            for r in rows:
                net_buy = float(r.get("buy_in", 0) or 0)
                total_buy = float(r.get("turnover", 0) or 0)
                total_sell = float(r.get("capitalization", 0) or 0)
                code = r.get("stock_code") or r.get("code") or ""
                if not code:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO lhb_daily (code, date, net_buy_amount, total_buy, total_sell) VALUES (?, ?, ?, ?, ?)",
                    (code, input.date, net_buy, total_buy, total_sell),
                )
                total += 1
            status = "success"
        elif fetch_error:
            # 源契约不可得 (熔断/字段契约不满足) → failed, 不静默写全零占位
            errors.append(fetch_error)
            status = "failed"
        else:
            # 源干净返回空 (可能尚未发布) → partial, 既有 benign 语义
            errors.append(f"LHB: 所有源均无数据 (date={input.date})")
            status = "partial"

        conn.commit()
        return PipelineResult(name=self.name, date=input.date, status=status,
                              records_written=total, errors=errors, warnings=warnings)

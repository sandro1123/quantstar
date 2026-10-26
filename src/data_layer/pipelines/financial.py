"""pipelines/financial.py — 财务采集管道（通过 SourceRegistry 获取数据源）"""
import calendar

from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.repos import get_repo

logger = get_logger("data.financial")


def _norm_iso_date(value) -> str:
    s = str(value or "")
    compact = s.replace("-", "")
    if len(compact) == 8 and compact.isdigit():
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    return s


class FinancialPipeline(BasePipeline):
    name = "financial"
    requires = ["stock_info"]
    produces = ["stock_financials"]

    def __init__(self, source_registry=None):
        self.source_registry = source_registry

    def _fetch_financial(self, code: str, report_date: str) -> dict | None:
        if not self.source_registry:
            return None
        sources = self.source_registry.get_sources_for("financial")
        for source in sources:
            try:
                fin = source.get_data("financial", {"code": code, "date": report_date})
                if fin:
                    return fin
            except Exception:
                continue
        return None

    def run(self, input: PipelineInput) -> PipelineResult:
        repo = get_repo(input.db)
        symbols = input.symbols or repo.stock.get_all_active()
        errors = []
        total = 0
        year = int(input.date[:4])
        month = int(input.date[5:7])
        last_day = calendar.monthrange(year, month)[1]
        report_date = input.date.replace("-", "")[:6] + f"{last_day:02d}"

        for code in symbols:
            try:
                fin = self._fetch_financial(code, report_date)
                if fin is None:
                    continue
                repo.financial.batch_upsert([{
                    "code": fin.get("code", code), "date": _norm_iso_date(fin.get("date", input.date)),
                    "eps": fin.get("eps", 0), "roe": fin.get("roe", 0),
                    "revenue": fin.get("revenue", 0), "profit": fin.get("profit", 0),
                    "total_assets": fin.get("total_assets", 0), "total_liabilities": fin.get("total_liabilities", 0),
                    "net_assets": fin.get("net_assets", 0), "operating_cash_flow": 0,
                    "gross_margin": fin.get("gross_margin", 0), "net_margin": fin.get("net_margin", 0),
                }])
                total += 1
            except Exception as e:
                errors.append(f"{code}: {e}")

        status = "success" if not errors else ("partial" if total > 0 else "failed")
        return PipelineResult(name=self.name, date=input.date, status=status, records_written=total, errors=errors)

"""pipelines/moneyflow.py — 资金流数据管道（同花顺 10jqka 源）"""
from common.logging import get_logger
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.repos import get_repo

logger = get_logger("data.moneyflow")


class MoneyflowPipeline(BasePipeline):
    name = "moneyflow"
    requires = ["stock_info"]
    # W2 卡④: produces 声明真实表名 (原 "moneyflow" 是逻辑名, 与
    # repos/moneyflow.py 实际落 stock_moneyflow 错位 — 契约面对拍失败)。
    produces = ["stock_moneyflow"]

    def __init__(self, source_registry=None):
        self.source_registry = source_registry

    def _get_all_flows(self) -> dict[str, dict]:
        """通过 SourceRegistry 批量获取全市场资金流数据，返回 {code: data}"""
        if not self.source_registry:
            return {}
        sources = self.source_registry.get_sources_for("moneyflow")
        if not sources:
            return {}
        for source in sources:
            if hasattr(source, "get_all_moneyflow"):
                try:
                    data = source.get_all_moneyflow()
                    if data:
                        return data
                except Exception as e:
                    logger.warning(f"Bulk moneyflow failed from {source.name}: {e}")
                    continue
        return {}

    def run(self, input: PipelineInput) -> PipelineResult:
        from common.utils import bj_date

        if input.date != bj_date():
            return PipelineResult(
                name=self.name, date=input.date, status="failed",
                records_written=0,
                errors=["moneyflow source only provides the current trading-day snapshot"],
            )
        if not self.source_registry:
            return PipelineResult(name=self.name, date=input.date, status="skipped",
                records_written=0, errors=["no source_registry"])

        sources = self.source_registry.get_sources_for("moneyflow")
        if not sources:
            logger.warning("No available moneyflow sources")
            return PipelineResult(name=self.name, date=input.date, status="skipped",
                records_written=0, errors=["no available moneyflow sources"])

        repo = get_repo(input.db)
        symbols = input.symbols or repo.stock.get_all_active()
        errors = []
        total = 0

        all_flows = self._get_all_flows()
        if not all_flows:
            return PipelineResult(name=self.name, date=input.date, status="failed",
                records_written=0, errors=["failed to fetch fund flow data"])

        for code in symbols:
            try:
                data = all_flows.get(code)
                if not data:
                    continue
                repo.moneyflow.batch_upsert([{
                    "code": code, "date": input.date,
                    "main_net_inflow": data["main_net_inflow"],
                    "retail_net_inflow": 0.0,
                    "main_inflow": data["main_inflow"],
                    "main_outflow": data["main_outflow"],
                }])
                total += 1
            except Exception as e:
                errors.append(f"{code}: {e}")

        status = "success" if not errors else ("partial" if total > 0 else "failed")
        return PipelineResult(name=self.name, date=input.date, status=status, records_written=total, errors=errors)

"""pipelines/fuyao.py — Fuyao 财务数据采集管道 (4 API 合并)"""


import logging
from datetime import datetime, timedelta, timezone

# BJT = A 股市场本地时间 (UTC+8)。period_end_ms/report_date_ms 是 BJT 毫秒戳,
# UTC 渲染会把报告期末 06-30 16:00 BJT 落成 06-29 → date 键 off-by-one
# (财务修法①, fin_source_card 病A; Q2 批 5546 行 06-29 病键实证)。
BJT = timezone(timedelta(hours=8))

from data_layer._models import Financial
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult
from data_layer.repos import get_repo
from data_layer.sources.fuyao_client import FuyaoClient, _fallback_reports

logger = logging.getLogger("data.fuyao_financial")


def _thscode(code: str) -> str:
    # 微卡① (09-05): market 感知 — BJ 段 (北交所 92/43/83/87 前缀) 映 .BJ。
    # 原单行实现把 43/83/87/92 全映 .SZ → fuyao 源全 1002, BJ 全史 0 覆盖
    # (591 只结构性缺口, #115 普查)。92 前缀夜窗先验已过 (.BJ 全服 code=0);
    # 43/83/87 修后仍零数据 = 源头缺口, 由 DL-76 indicators_missing 诚实计数。
    if code.startswith(("92", "43", "83", "87")):
        return f"{code}.BJ"
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def _safe_float(v, default=0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _extract_indicator(abilities: list[dict], index_id: str) -> float:
    for ability in abilities:
        for ind in ability.get("indicators", []):
            if ind.get("index_id") == index_id:
                return _safe_float(ind.get("value"))
    return 0.0


def _detect_best_report(client: FuyaoClient, probe_code: str = "000001") -> str | None:
    ths = _thscode(probe_code)
    for rpt in _fallback_reports():
        result = client._get("/api/a-share/financials/income-statements", {
            "thscode": ths, "period": "quarterly", "limit": 1, "report": rpt,
        })
        data = result.get("item", []) if isinstance(result, dict) else []
        if data:
            return rpt
    return None


def _report_period_date(client: FuyaoClient, report: str, probe_code: str = "000001") -> str | None:
    """解析指定报告期的报告期末日期 (ISO)，用于增量覆盖判断。"""
    ths = _thscode(probe_code)
    result = client._get("/api/a-share/financials/income-statements", {
        "thscode": ths, "period": "quarterly", "limit": 1, "report": report,
    })
    data = result.get("item", []) if isinstance(result, dict) else []
    if not data:
        return None
    period_ms = data[0].get("period_end_ms")
    if not period_ms:
        return None
    return datetime.fromtimestamp(float(period_ms) / 1000, tz=BJT).strftime("%Y-%m-%d")


class FuyaoPipeline(BasePipeline):
    name = "fuyao_financial"
    requires = ["stock_info"]
    produces = ["stock_financials"]

    def __init__(self):
        self._client: FuyaoClient | None = None

    @property
    def client(self) -> FuyaoClient:
        if self._client is None:
            self._client = FuyaoClient()
        return self._client

    def run(self, input: PipelineInput) -> PipelineResult:
        repo = get_repo(input.db)
        symbols = input.symbols or repo.stock.get_all_active()
        warnings = []
        errors = []
        total = 0
        indicators_missing = 0  # DL-76: 该季度 indicators 未发布而跳过写入的计数

        if not symbols:
            return PipelineResult(
                name=self.name, date=input.date,
                status="success", records_written=0,
                warnings=["无活跃股票"],
            )

        if not self.client.api_key:
            return PipelineResult(
                name=self.name, date=input.date,
                status="failed", errors=["FUYAO_API_KEY 未设置"],
            )
        if not self.client.available():
            # available() 亦在熔断器打开时返回 False (上游瞬时故障, 非缺 key)。
            # 区分报错, 避免把 API 抖动误报成 "KEY 未设置"。
            return PipelineResult(
                name=self.name, date=input.date,
                status="failed",
                errors=["Fuyao API 暂不可用 (熔断保护中, 上游抖动, 稍后自动恢复)"],
            )

        best_report = _detect_best_report(self.client)
        if not best_report:
            # QS 卡 financials-q3-window-guard 件④ (2026-10-02): 全列表探测失败
            # 区分显形 — 四端点全拒 (code=1002 端点级失效, 08-31 批量后权限/配额
            # 疑失效) 与「报告期未披露」现同貌 (partial rows=0), 静默无痕。
            # 探测序列全空=源端问题, failed + 显性错误 (兜底卡/换源卡的触发锚)。
            probe_err = "fuyao financials 探测序列全拒 (四端点 code=1002), 权限/配额疑失效, 最后批量 08-31 — 兜底源卡触发"
            logger.warning(probe_err)
            return PipelineResult(
                name=self.name, date=input.date,
                status="failed", errors=[probe_err],
                metadata={"probe_reports": _fallback_reports()[:5]},
            )

        # 方案A: 增量跳过。财务为季度数据，仅在最新报告期未覆盖时拉取。
        # 先探测最新报告期的报告期末日期，再比对已有覆盖。
        target_period = _report_period_date(self.client, best_report)
        if not target_period:
            return PipelineResult(
                name=self.name, date=input.date,
                status="success", records_written=0,
                errors=[], warnings=[f"无法解析报告期 {best_report} 的报告期末日，跳过"],
                metadata={"report": best_report},
            )

        # 查询 stock_financials 中已覆盖该报告期末日的股票集合（date 为 ISO 季度末）
        covered = set()
        try:
            covered = repo.financial.codes_for_date(target_period)
        except Exception as e:
            warnings.append(f"查询已覆盖集合失败: {e}")

        missing = [c for c in symbols if c not in covered]

        # 未显式指定 symbols（全市场调度）时：若最新报告期已全量覆盖，则快速跳过。
        if not input.symbols and not missing:
            return PipelineResult(
                name=self.name, date=input.date,
                status="success", records_written=0,
                warnings=[f"报告期 {best_report}（{target_period}）已覆盖全部 {len(symbols)} 只，跳过"],
                metadata={"skipped": True, "report": best_report, "target_period": target_period},
            )

        for code in missing:
            try:
                ths = _thscode(code)

                income = self.client.get_income_statements(ths, report=best_report)
                eps, revenue, profit = 0.0, 0.0, 0.0
                if income:
                    eps = _safe_float(income[0].get("basic_eps"))
                    revenue = _safe_float(income[0].get("operating_income"))
                    profit = _safe_float(income[0].get("parent_holder_net_profit"))

                balance = self.client.get_balance_sheets(ths, report=best_report)
                total_assets, total_liabilities, net_assets = 0.0, 0.0, 0.0
                if balance:
                    total_assets = _safe_float(balance[0].get("assets_total"))
                    total_liabilities = _safe_float(balance[0].get("total_debt"))
                    net_assets = _safe_float(balance[0].get("holder_equity_total"))

                cashflow = self.client.get_cash_flow(ths, report=best_report)
                operating_cash_flow = 0.0
                if cashflow:
                    operating_cash_flow = _safe_float(cashflow[0].get("act_cash_flow_net"))

                indicators = self.client.get_financial_indicators(ths, report=best_report)
                # DL-76: indicators 为空 (该季度未发布) 时不写记录 — 写 roe=0/gm=0
                # 等于把"不知道"写成 0.0, 会让 roe_quality 等因子喂上垃圾值。
                # 跳过并留可查询计数 (铁律 #12, 不静默降级)。
                if not indicators or not indicators.get("abilities"):
                    indicators_missing += 1
                    continue
                abilities = indicators.get("abilities", [])
                roe = _extract_indicator(abilities, "index_weighted_avg_roe")
                gross_margin = _extract_indicator(abilities, "sale_gross_margin")
                net_margin = _extract_indicator(abilities, "sale_net_interest_ratio")

                if eps == 0 and revenue == 0 and profit == 0:
                    continue

                period_ms = income[0].get("period_end_ms") if income else None
                if not period_ms:
                    continue
                period_date = datetime.fromtimestamp(
                    float(period_ms) / 1000, tz=BJT
                ).strftime("%Y-%m-%d")
                if period_date > input.date:
                    continue

                # DL-47: 公告日 = report_date_ms (PIT 过滤依赖此字段)
                announce_ms = income[0].get("report_date_ms") if income else None
                announce_date = (
                    datetime.fromtimestamp(float(announce_ms) / 1000, tz=BJT).strftime("%Y-%m-%d")
                    if announce_ms else ""
                )

                fin = Financial(
                    code=code, date=period_date,
                    eps=eps, roe=roe,
                    revenue=revenue, profit=profit,
                    gross_margin=gross_margin, net_margin=net_margin,
                )
                repo.financial.batch_upsert([{
                    "code": fin.code, "date": fin.date,
                    "eps": fin.eps, "roe": fin.roe,
                    "revenue": fin.revenue, "profit": fin.profit,
                    "total_assets": total_assets,
                    "total_liabilities": total_liabilities,
                    "net_assets": net_assets,
                    "operating_cash_flow": operating_cash_flow,
                    "gross_margin": fin.gross_margin, "net_margin": fin.net_margin,
                    "announce_date": announce_date,
                    "caliber": "cumulative",
                }])
                total += 1
            except Exception as e:
                errors.append(f"{code}: {e}")

        # DL-76: indicators 缺失留痕 — 计数进 metadata, 全缺时降级 partial + 告警。
        if indicators_missing:
            warnings.append(
                f"{indicators_missing} 只因报告期 {best_report} indicators 未发布被跳过写入"
            )
        if errors and total == 0:
            status = "failed"
        elif errors or (indicators_missing and total == 0):
            status = "partial"
        else:
            status = "success"
        # QS 卡 financials-q3-window-guard 件② (2026-10-02): 覆盖洞按板块显形。
        # 268 只活跃股 (北交所 250+沪深 18) 无 Q2 行 — 增量闸只报 missing 总数,
        # 北交所结构性洞 (源端 BJ indicators 恒缺) 被总数稀释静默。
        # 按 code 前缀分组计数进 metadata (P1-4 bj_legacy_skipped 先例), 不强行拉。
        missing_by_board: dict[str, int] = {}
        for code in missing:
            missing_by_board[code[:2]] = missing_by_board.get(code[:2], 0) + 1
        return PipelineResult(
            name=self.name, date=input.date,
            status=status, records_written=total, errors=errors,
            warnings=warnings,
            metadata={"report": best_report, "target_period": target_period,
                       "skipped_count": len(covered), "missing": len(missing),
                       "indicators_missing": indicators_missing,
                       "missing_by_board": missing_by_board},
        )

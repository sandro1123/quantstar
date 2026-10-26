"""NewsPipeline — 把 NewsCollector 包装为 BasePipeline 并注册到 dispatch"""

from __future__ import annotations

from common.logging import get_logger
from config_layer import cfg_get
from data_layer.news_attribution import audit_attribution
from data_layer.news_collector import NewsCollector
from data_layer.news_stock_links import (
    backfill_news_stock_links,
    second_pass_title_scan,
)
from data_layer.pipelines.base import BasePipeline, PipelineInput, PipelineResult

logger = get_logger("data.pipelines.news")


def _audit_config() -> dict:
    """#56 卡B: 归属哨兵配置 (news.attribution_audit.enforce)。

    哨兵宪法 — 默认关: 配置段缺失/读取失败/解析失败一律 {"enforce": False},
    不猜不默认开 (宁可NULL不可假值同源纪律)。

    正解 API = config_layer (cfg_get 点键读, llm 域同款)。初版误用
    `from common.config import Config` — 该模块不存在 (common 包无
    config.py, 生产/工tree 双证 ModuleNotFoundError), 永远走 except
    返回 False, 哨兵开不动。坏 import 由哨兵段 try 吞掉不可见, 故
    TDD 补真配置链测试 (非 monkeypatch 直喂值) 才暴露。
    """
    try:
        return {"enforce": bool(cfg_get("news.attribution_audit.enforce", False))}
    except Exception:
        return {"enforce": False}


class NewsPipeline(BasePipeline):
    name = "stock_news"
    requires = []
    produces = ["stock_news"]

    def __init__(self, db=None, llm_service=None):
        self.db = db
        self.llm_service = llm_service

    def run(self, input: PipelineInput) -> PipelineResult:
        collector = NewsCollector(db=self.db, llm_service=self.llm_service)
        date = input.date
        errors = []
        records_written = 0
        try:
            # AG-NEWS ④a: 候选池透传 (PipelineInput.symbols) — 夜窗首真拉面。
            # 无 symbols 不拉个股腿 (防全市场 5500×10 行打爆源)。
            records_written = collector.collect(date, codes=input.symbols)
        except Exception as e:
            errors.append(f"NewsCollector.collect failed: {e}")
        metadata = {"source": "NewsCollector"}
        # #56 卡B: 归属口径哨兵 (report-only) — 采集尾 piggyback。
        # 段内 try 不杀主管道 (06fc3b3 decay 哨兵先例): 哨兵失败只 warning,
        # 不进 errors 不改 status; 默认关 (enforce=false), 开态产物只落
        # metadata (attribution_dist / vocab_drift / violations 三件口径)。
        try:
            if _audit_config().get("enforce"):
                metadata["attribution_audit"] = audit_attribution(
                    self.db, date, enforce=True
                )
        except Exception as audit_exc:
            logger.warning(
                f"attribution_audit 哨兵段失败 (不影响主管道): {audit_exc}"
            )
        # AG-NEWS links 通电 (甲案, 统筹 sm-4cff88c5 GO): run 尾哨兵式双调 —
        # backfill (归属行增量灌 links, 幂等) + second_pass (per_stock_em 行
        # 标题全名扫描多股关联)。发现B定谳: 此前 links 整表写面断供 (零调用),
        # 09-12 首 240 对=手动一次性产物。段内 try 不杀主管道 (同上哨兵先例):
        # 失败只 warning + metadata 留痕, 不进 errors 不改 status — 增强面
        # 故障不得杀主采集管道 (纪律③: 降级必留痕)。
        try:
            if self.db is not None:
                conn = self.db.get_connection()
                metadata["links_backfilled"] = backfill_news_stock_links(conn)
                metadata["links_title_scan_2nd"] = second_pass_title_scan(conn)
        except Exception as links_exc:
            logger.warning(f"links 哨兵段失败 (不影响主管道): {links_exc}")
            metadata["links_warning"] = f"backfill/second_pass degraded: {links_exc}"
        return PipelineResult(
            name=self.name,
            date=date,
            status="success" if not errors else "partial",
            records_written=records_written,
            errors=errors,
            metadata=metadata,
        )

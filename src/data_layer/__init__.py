# ruff: noqa: F401
"""data_layer/__init__.py — DataLayer 入口"""

from __future__ import annotations

from common.database import Database
from common.decorators import catch_quantstar_error, with_timeout
from common.logging import get_logger
from common.utils import generate_trace_id, validate_identifier
from config_layer import Config
from data_layer._loader import DataLoader
from data_layer._models import MinuteBar, SentimentData, StockInfo
from data_layer.evolution.schema import SchemaEvolution
from data_layer.health.readiness import (
    DataReadiness,
    fiscal_stale_days,
    fiscal_window_ok,
)
from common.calendar import TradingCalendar
from data_layer.pipelines.adjust_factor import AdjustFactorPipeline
from data_layer.pipelines.analyst_forecast import AnalystForecastPipeline
from data_layer.pipelines.auction_collect import AuctionCollectPipeline
from data_layer.pipelines.concept import ConceptPipeline
from data_layer.pipelines.concept_plates import ConceptPlatesPipeline
from data_layer.pipelines.concept_intervals import ConceptIntervalsPipeline
from data_layer.pipelines.financial import FinancialPipeline
from data_layer.pipelines.freshness_audit import FreshnessAuditPipeline
from data_layer.pipelines.fuyao import FuyaoPipeline
from data_layer.pipelines.index_daily import IndexDailyPipeline
from data_layer.pipelines.kline import KlinePipeline
from data_layer.pipelines.lhb import LHBPipeline
from data_layer.pipelines.limit_up_down import LimitUpDownPipeline
from data_layer.pipelines.moneyflow import MoneyflowPipeline
from data_layer.pipelines.news import NewsPipeline
from data_layer.pipelines.quality_check import QualityCheckPipeline
from data_layer.pipelines.registry import PipelineRegistry
from data_layer.pipelines.sentiment import SentimentPipeline
from data_layer.pipelines.shortterm_collect import ShortTermCollectPipeline
from data_layer.pipelines.shortterm_recollect import ShortTermRecollectPipeline
from data_layer.pipelines.source_crosscheck import SourceCrosscheckPipeline
from data_layer.pipelines.stock_list import StockListPipeline
from data_layer.pipelines.suspend_recovery import SuspendRecoveryPipeline
from data_layer.pipelines.valuation import ValuationPipeline
from data_layer.pit import PITDataProvider, PITQuery, PITResult
from data_layer.sources.fusion import FusionStrategy
from data_layer.sources.fuyao_client import FuyaoClient
from data_layer.sources.registry import SourceRegistry

logger = get_logger("data")


class DataLayer:
    def __init__(self, db: Database | None = None, config: Config | None = None):
        self.config = config
        if db is not None:
            self.db = db
        elif config is not None and config.get("data.db_path"):
            self.db = Database(config.get("data.db_path"))
        else:
            self.db = Database()
        self.loader = DataLoader(self.db)
        self.pipelines = PipelineRegistry()
        self._trace_id = generate_trace_id()

        fuyao_base_url = (
            self.config.get("data.sources.fuyao.base_url", "https://fuyao.aicubes.cn")
            if self.config else "https://fuyao.aicubes.cn"
        )
        self.fuyao_client = FuyaoClient(base_url=fuyao_base_url)

        # 新增：Source 组件

        self._source_registry = SourceRegistry()
        # DL-01: 多源融合策略 — 管线经 fuse() 取数并交叉验证 (D196-D198)
        self._fusion = FusionStrategy(self._source_registry)
        self._schema_evolution = SchemaEvolution(self.db)
        self._pit = PITDataProvider(self.db)

        self._register_pipelines()
        self._register_sources()
        # D3 第二段: 源注册完成后回注 suspend_recovery 的备源 (baostock —
        # trade_status 批量判+get_daily_market 快照同源)。
        _sr_pipe = self.pipelines.get("suspend_recovery")
        if _sr_pipe is not None and _sr_pipe.source is None:
            _bs = [s for s in self._source_registry.get_sources_for("kline_bulk")
                   if s.name == "baostock"]
            if _bs:
                _sr_pipe.source = _bs[0]

    def _register_pipelines(self):
        self.pipelines.register(KlinePipeline(source_registry=self._source_registry))
        self.pipelines.register(FinancialPipeline(source_registry=self._source_registry))
        self.pipelines.register(ValuationPipeline(source_registry=self._source_registry))
        self.pipelines.register(MoneyflowPipeline(source_registry=self._source_registry))
        self.pipelines.register(FuyaoPipeline())
        self.pipelines.register(ConceptPipeline(source_registry=self._source_registry))
        # DL-49: 概念/题材板块正向映射 (zzshare plates)。🔴 GO-7 硬闸: 只注册进
        # PipelineRegistry 供单独调起/回补, **不接进 orchestrator sub_pipelines**
        # (写锁血栓: 日管道持写锁 22-40min 撞崩 factor_update), 接入等 GO-7 排期。
        self.pipelines.register(ConceptPlatesPipeline(source_registry=self._source_registry))
        # 区间存储 (统筹 sm-337ab3642f7f486f 批准): concept_membership +
        # plate_daily_rate。🔴 GO-7 硬闸同 concept_plates: 只注册进 PipelineRegistry,
        # 不接进 orchestrator sub_pipelines; 生产建表/写入/回补 (D-104) 另行 GO。
        self.pipelines.register(ConceptIntervalsPipeline(source_registry=self._source_registry))
        self.pipelines.register(LHBPipeline(source_registry=self._source_registry, fusion=self._fusion))
        self.pipelines.register(SentimentPipeline(source_registry=self._source_registry))
        self.pipelines.register(AnalystForecastPipeline(source_registry=self._source_registry))
        self.pipelines.register(NewsPipeline(db=self.db))
        self.pipelines.register(LimitUpDownPipeline(db=self.db, source_registry=self._source_registry, fusion=self._fusion))
        self.pipelines.register(StockListPipeline())
        # D3 第二段 (sm-46287f4038b242a3): 停牌复牌检测+K线补全常态化。
        # 🔴 GO-7 硬闸同 concept_plates: 只注册进 PipelineRegistry 供单独调起
        # (回补/夜间窗), 不接进 orchestrator sub_pipelines — 首真拉/挂行归统筹窗面。
        # source 在 _register_sources 后注入 (见 __init__ 尾) — 此处 registry 尚空
        # (坑: 管道注册先于源注册, 注册期取实例必得 None)。
        self.pipelines.register(SuspendRecoveryPipeline(source=None))
        # DL-07: 不可回填型情绪/短线结构冷采集 (不进选股链路)
        self.pipelines.register(ShortTermCollectPipeline(db=self.db, source_registry=self._source_registry))
        # 刀2 (fix/anomaly-1630-recollect): shortterm_recollect 16:30 补采轮
        self.pipelines.register(ShortTermRecollectPipeline(db=self.db, source_registry=self._source_registry))
        self.pipelines.register(AuctionCollectPipeline(db=self.db, source_registry=self._source_registry))
        # DL-11: DataQuality 接线 — 每日管线末端跑质量门禁, 结果落 quality_checks (D98-D99)
        self.pipelines.register(QualityCheckPipeline(db=self.db))
        # DL-12: DataReadiness 接线 — 每日管线末端跑新鲜度审计, 结果落 freshness_audit (D98-D100)
        self.pipelines.register(FreshnessAuditPipeline(db=self.db))
        # DL-23: 指数日线 (沪深300/创业板指), BT-04 基准对比硬前置
        self.pipelines.register(IndexDailyPipeline())
        # DL-35: 复权因子/分红/送转 每日增量 (独立管道, 18:30 跑, 见管道 docstring)
        self.pipelines.register(AdjustFactorPipeline(source_registry=self._source_registry))
        # P0-5: 源健康第三方对拍 (新浪参照), 抽样核对主源, 差异落 source_crosscheck_diffs
        self.pipelines.register(SourceCrosscheckPipeline(db=self.db))
        # 丙件 (2026-09-25): 估值顶层补跑腿 — 主跑残缺时逐股兜底。
        # 与 valuation 主跑分立: 主跑走 bulk + expected 门禁 (断供日必失败),
        # 本腿走逐股兜底源 (baostock) 只补缺口。
        from data_layer.pipelines.valuation_backfill import (
            ValuationBackfillPipeline,
        )
        self.pipelines.register(
            ValuationBackfillPipeline(source_registry=self._source_registry))

    def _register_sources(self):
        """注册所有数据源"""
        from data_layer.sources.baostock_source import BaoStockSourceAdapter
        from data_layer.sources.akshare_sina_source import AkshareSinaAdapter
        from data_layer.sources.akshare_source import AkShareSourceAdapter
        from data_layer.sources.easy_tdx_source import EasyTDXSourceAdapter
        from data_layer.sources.fuyao_client import FuyaoSourceAdapter
        from data_layer.sources.gtimg_source import GTimgSourceAdapter
        from data_layer.sources.mootdx_source import MootdxSourceAdapter
        from data_layer.sources.tonghuashun_source import TonghuashunSourceAdapter
        from data_layer.sources.tushare_source import TushareSourceAdapter
        from data_layer.sources.zzshare_source import ZZShareSourceAdapter
        from data_layer.sources.adata_source import ADataSourceAdapter
        from data_layer.sources.easyquotation_source import EasyQuotationSourceAdapter

        # 优先级按 DL-00 实测比武报告 (docs/source_benchmark_2026-08-18.md) 修正:
        # - fuyao 特色数据(涨跌停/龙虎榜/热榜/异动)全部 <0.4s, 免费权威 → 这些类型 priority=1
        # - tushare 唯一批量 kline 源 (5539只/2s), 限额内省用 → kline/valuation priority=1
        # - gtimg 免费快速 realtime_quote/index → 这些类型 priority=1
        # - akshare 东财 kline 仍被拒, 仅 limit_up_pool 可用 → limit_up 备源
        # - baostock 已移除 (DL-09): 实测登录返回 10001011 黑名单用户, 用户预决策移除
        # - easy_tdx 保留注册: 唯一 tick/分钟本地源, 本机不可用时 is_available() 门控自动排除 (DL-09)
        self._source_registry.register(
            FuyaoSourceAdapter(), priority=10,
            type_priorities={"limit_up": 1, "limit_down": 1, "financial": 1,
                             "hot_stock": 1, "anomaly": 1, "realtime_quote": 2,
                             "lhb": 1,
                             # DL-07: 不可回填型情绪/短线结构数据唯一源
                             "limit_ladder": 1, "limit_break": 1,
                             "hot_stock_history": 1, "skyrocket": 1,
                             "auction_benchmark": 1, "auction_snapshot": 1,
                             # DL-31: 三张财务报表 (fuyao 唯一可用源, tushare 无权限)
                             "income_statement": 1, "balance_sheet": 1, "cash_flow": 1})
        self._source_registry.register(
            TushareSourceAdapter(), priority=20,
            type_priorities={"kline": 1, "kline_bulk": 1, "valuation": 1,
                             "historical_valuation": 1, "stock_list": 1})
        self._source_registry.register(
            GTimgSourceAdapter(), priority=50,
            type_priorities={"realtime_quote": 1, "index": 1})
        self._source_registry.register(AkshareSinaAdapter(), priority=2)
        # akshare fallback 修法卡 (09-28): 逐股 kline 链第三位 — akshare_sina 空
        # → easyquotation(sina) 接力, 不静默断流。kline=2 与 akshare_sina 同级,
        # 注册序决定先后 (registry sort 稳定 + dict 插入序)。无历史日线接口,
        # 只补当日格; 历史段仍靠 tushare/baostock。is_available 软门控 — 包未装
        # 自动排除不炸注册表 (同 easy_tdx DL-09 模式)。
        self._source_registry.register(
            EasyQuotationSourceAdapter(), priority=4,
            type_priorities={"kline": 2})
        self._source_registry.register(MootdxSourceAdapter(), priority=3)
        self._source_registry.register(TonghuashunSourceAdapter(), priority=5)
        self._source_registry.register(
            AkShareSourceAdapter(), priority=30,
            type_priorities={"limit_up": 2, "limit_down": 2,
                             # DL-07: 板块资金流唯一源
                             "sector_fund_flow": 1,
                             # FINANCIALS 注册面补丁 (手术卡 FINANCIALS_SURGERY_CARD_20260930)
                             # fuyao(1) 主源当季洞/源端拒时, financial 兜底走 akshare 东财
                             # _get_financial = stock_financial_analysis_indicator
                             # 金灯实证 2026-09-30 (600519/000001 rows=6 elapsed 0.19/0.27s 字段齐)
                             "financial": 2})
        self._source_registry.register(ZZShareSourceAdapter(), priority=35)
        # 卡#20 第一段 (sm-64aaee3df477431b): adata 免费源 — 单源对冲+情绪原料。
        # lhb: fuyao(1) 主源即时对冲备源(2) — fuyao lhb_daily 滞后 09-04 实证;
        # popularity/concept_hot: 唯一源 (b案单腿/ths); mine_clearance: 唯一源。
        # 北向 (个股级停披全0) / 两融 (API 空) 不入册 (统筹 09-09 定谳)。
        self._source_registry.register(
            ADataSourceAdapter(), priority=25,
            type_priorities={"lhb": 2, "popularity": 1, "concept_hot": 1,
                             "mine_clearance": 1})
        self._source_registry.register(EasyTDXSourceAdapter(), priority=45)
        # DL-35/D-39: baostock 恢复接入 (2026-08-22 复测证伪 DL-09 黑名单结论)。
        # DL-25: kline_bulk=2 → tushare(1) 主, baostock(2) 备 (注册表可见降级,
        # akshare_sina 无 get_daily_market 批量能力, 天然不在 bulk 路径)。
        # 逐股 kline=3 置于 akshare_sina(全局2) 之后: baostock 逐股走有状态登录
        # + 全局锁, 在 8-worker 逐股路径里作最后兜底更稳, 避免锁串行拖慢
        # (回执已说明此排序取舍)。
        self._source_registry.register(
            BaoStockSourceAdapter(), priority=40,
            type_priorities={"adjust_factor": 1, "dividend": 1,
                             "kline_bulk": 2, "kline": 3,
                             # 丙件: 估值兜底。tushare(1)/akshare(30 未声明 capability)
                             # → baostock(5) 为补跑腿的唯一逐股估值源
                             "valuation": 5})

    def start(self):
        pass

    @catch_quantstar_error(layer_name="data")
    def run_pipeline_holiday_gate(self, name: str, input) -> dict | object:
        """QS 卡 holiday-early-exit-gate (Day2, 2026-10-07): 入口假日早退。

        假日 (TradingCalendar False) + 非白名单管道 → 直接落 skipped dict
        (reason 含 'non-trading day' + 当日日期, T1 硬约束: 假日 skip 须
        可见痕迹, 判例 #147 反面「假日绿≠源挂绿」)。零业务执行零超时开销。
        白名单 (ops.holiday_exempt_pipelines) 假日照跑 (T3)。
        交易日原路径 (T2) — 落 pipeline_runs 由调用方 ops 层负责, 本 gate
        只返回判定结果形态。
        """
        from data_layer.holiday_gate import should_holiday_skip, skipped_reason
        from common.utils import bj_date
        today = getattr(input, "date", None) or bj_date()
        if should_holiday_skip(self.db, name, today):
            reason = skipped_reason(today)
            logger.warning(f"holiday_gate: {name} 假日早退 {reason}")
            return {"status": "skipped", "error_msg": reason,
                    "records_written": 0, "holiday_gate": True}
        return self.run_pipeline(name, input)

    def run_pipeline(self, name: str, input) -> object:
        """Run a pipeline with automatic source fallback on failure.

        Kline/估值/财务/资金流均走全市场逐股拉取，为慢批量；默认超时由
        config ``timeout_s`` 提供，未指定时按管道名抽长窗口(1200-1800s)
        以避免 60s 短超时导致每日管道被截断而断档。
        """
        _timeout = 60
        try:
            _capability_timeout = {
                # 全市场逐股/逐源拉取为慢批量，均需长窗口避免 60s 短超时截断断档
                "valuation": 1800,
                "kline": 1800,
                "fuyao_financial": 1800,
                "moneyflow": 1200,
                # analyst_forecasts 逐码调 akshare HTTP 拉 ~5400 只, 实测需 ~30min (1850s);
                # 60s 默认超时必被截断 → with_timeout 返回 None → 误报 "returned no result"
                # (2026-08-17 事故: 幽灵线程 16:12 才跑完写入 2864 行, 但 16:01 已误报失败)
                # DDP-BUDGET-FIT: 1200→1900 — 实测完成需 ~1850s (08-28 两次跑 ghost 均 ~31min);
                # DDP c-1 (#81) 后: analyst 拆 16:55 独立顶层管道, 本 1900 与
                # ops.yaml analyst_forecasts.timeout_s 双钉对齐 (不占 DDP 2700 组成)
                "analyst_forecasts": 1900,
                # DL-07: 冷采集管线 — 逐端点串行, fuyao 各端点 <0.4s + akshare ~10s
                "shortterm_collect": 300,
                "auction_collect": 120,
                # adjust_factor: baostock 腿 ~2s + mootdx xdxr 交叉验证(抽样≤10) 晚窗
                # 单调用实测 ~14s (每调用重触发 bestip 选服环); 单进程完整墙钟实测
                # 306/309s (08-31/08-28 晚 ghost 跑到底后落库)。旧 60s 默认先于
                # ops.yaml 300s 处决 → 误报"管线返回空结果"(幽灵迟到写, 同型 08-17 analyst)。
                # ADJFIX-BUDGET-FIT: 600 = 309×1.94 / (10×40+5)×1.48, 两口径≥30%裕度;
                # 外层 cron_watchdog ceiling 1140s (300×3+60×2) 内富余。
                # 取证: ~/qs-cfgsnap/audit/20260901-redcluster-E/adjfix/ (p1~p9)
                "adjust_factor": 600,
            }
            _timeout = int(
                getattr(input, "config", {}).get(
                    "timeout_s",
                    _capability_timeout.get(name, 60),
                )
            )
        except Exception:
            _timeout = 60
        return with_timeout(timeout_s=_timeout)(self._run_pipeline_impl)(name, input)

    def _run_pipeline_impl(self, name: str, input) -> object:
        if hasattr(input, 'db') and input.db is None:
            input.db = self.db
        logger = get_logger("data")
        # 检查数据是否已存在且有效（非零 change_pct）
        try:
            conn = self.db.get_connection()
            if name == "kline" or name == "daily_data_pipeline":
                date = getattr(input, 'date', '')
                symbols = getattr(input, 'symbols', None)
                # 短路判断只对”本次请求覆盖的符号集”生效:
                # - 指定了 symbols: 仅当这些符号当日都有有效行才跳过
                # - 未指定 symbols(全市场): 不对零星行短路, 必须由管道内增量逻辑决定
                if symbols:
                    phs = ",".join("?" for _ in symbols)
                    existing = conn.execute(
                        f"SELECT COUNT(*) FROM daily_klines WHERE date = ? AND code IN ({phs}) AND change_pct != 0",
                        (date, *symbols)
                    ).fetchone()[0]
                    if existing == len(symbols):
                        logger.info(f"符号集当日数据已存在且有效 ({existing}/{len(symbols)})，跳过管道 {name}")
                        return {}
        except Exception:
            pass
        result = self.pipelines.run(name, input)
        if hasattr(result, 'status') and result.status == 'failed':
            available = self._source_registry.get_available_sources()
            logger.warning(f"Pipeline {name} failed, {len(available)} fallback sources available")
        return result

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=10)
    def count_daily_klines(self, date: str) -> int:
        """当日 daily_klines 真实行数 (W1 no-stocks-obs 观测钉计数面)。

        只读 SELECT COUNT(*), 不取行体。db 面异常直接抛 (读不到 != 真的 0,
        调用方 no_stocks 分支标 fail_count 留痕 — 判例: 空集与零命中
        必须可区分)。date 空串返 0 (无当日可数, 不查全表防误读全库量)。
        """
        if not date:
            return 0
        conn = self.db.get_connection()
        return conn.execute(
            "SELECT COUNT(*) FROM daily_klines WHERE date = ?", (date,)
        ).fetchone()[0]

    def snapshot(self, date: str):
        return self.loader.snapshot(date)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=10)
    def snapshot_intraday(self, timestamp: str | None = None):
        return self.loader.snapshot_intraday(timestamp, source_registry=self._source_registry)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=15)
    def get_realtime_prices(self, codes: list[str]) -> dict[str, float]:
        """批量实时价 (F4.3) — 返回 {code: price}。

        优先 realtime_quote_batch 能力源 (gtimg 80只/次), 逐源降级;
        全部失败返回 {} 由调用方降级 daily_klines 昨收。
        """
        if not codes:
            return {}
        prices: dict[str, float] = {}
        remaining = list(dict.fromkeys(codes))
        for source in self._source_registry.get_sources_for("realtime_quote_batch"):
            if not remaining:
                break
            try:
                quotes = source.get_data("realtime_quote_batch", {"codes": remaining})
                for q in quotes or []:
                    code = q.get("code", "")
                    price = q.get("price", 0)
                    if code and price > 0 and code not in prices:
                        prices[code] = float(price)
                remaining = [c for c in remaining if c not in prices]
            except Exception:
                continue
        # 无批量能力的源: 逐只 realtime_quote 兜底 (仅少量缺失时)
        if remaining and len(remaining) <= 10:
            for source in self._source_registry.get_sources_for("realtime_quote"):
                if not remaining:
                    break
                for code in list(remaining):
                    try:
                        q = source.get_data("realtime_quote", {"code": code})
                        if q and q.get("price", 0) > 0:
                            prices[code] = float(q["price"])
                            remaining.remove(code)
                    except Exception:
                        continue
        return prices

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=15)
    def get_realtime_indices(self, codes: str | None = None) -> list[dict]:
        """DL-08: 实时大盘指数 — 收编报告层 gtimg 直连 (F-10④⑤⑥)。

        经 SourceRegistry 走 "index" 能力 (gtimg 主源), 返回
        [{"name", "price", "change_pct"}]; 源不可用/失败返回 [] 由调用方降级。
        """
        for source in self._source_registry.get_sources_for("index"):
            try:
                params = {"codes": codes} if codes else {}
                rows = source.get_data("index", params)
                if rows:
                    return rows
            except Exception:
                continue
        return []

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=15)
    def get_limit_pool(self, date: str) -> dict:
        """DL-40: 涨/跌停池读 API — 收编报告层 FuyaoClient 直连 (midday_alerts)。

        经 SourceRegistry 走 limit_up/limit_down 能力 (fuyao 主源, akshare 备源)。
        返回 {"up": [...], "down": [...]}; 源不可用/失败返回 {"up": [], "down": []}。
        """
        result = {"up": [], "down": []}
        for direction in ("up", "down"):
            for source in self._source_registry.get_sources_for(f"limit_{direction}"):
                try:
                    rows = source.get_data(f"limit_{direction}", {"date": date})
                    if rows:
                        result[direction] = rows
                        break
                except Exception:
                    continue
        return result

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=15)
    def get_sector_rotation(self, date: str = "") -> list[dict]:
        """DL-40: 板块轮动读 API — 收编报告层 FuyaoClient 直连 (midday_sectors)。

        经 SourceRegistry 走 index_catalog + index_snapshot 能力 (fuyao 主源)。
        返回 [{"name", "pct"}] 按涨幅降序; 源不可用/失败返回 []。
        """
        for source in self._source_registry.get_sources_for("index_catalog"):
            try:
                items = source.get_data("index_catalog", {"tag": "industry"})
                if not items:
                    continue
                top_codes = [it.get("thscode", "") for it in items[:80] if it.get("thscode")]
                if not top_codes:
                    continue
                snap = source.get_data("index_snapshot", {"codes": ",".join(top_codes)})
                snap_items = snap if isinstance(snap, list) else []
                if not snap_items:
                    continue
                name_map = {it.get("thscode", ""): it.get("name", "") for it in items}
                sectors = []
                for it in snap_items:
                    ts = it.get("thscode", "")
                    name = name_map.get(ts, ts)
                    pct = it.get("price_change_ratio_pct", 0) or 0
                    if name:
                        sectors.append({"name": name, "pct": round(pct, 2)})
                sectors.sort(key=lambda x: x["pct"], reverse=True)
                return sectors
            except Exception:
                continue
        return []

    @catch_quantstar_error(layer_name="data")
    # B9 补 (2026-10-21): 原独立硬编码 60s, 与报告层预算无共享 ——
    # 今早 08:00 日志第三个超时点就是它。晨报/午报路径都会走
    # (sections/news.py:27 / midday_news.py:17), 内层 60s 截断会让
    # 外层 180s 预算形同虚设。改为与外层同源键驱动。
    # 无键/非法值回落 180s 字面量 (与 build_morning_brief 一致)。
    @with_timeout(timeout_s=180, config_key="ops.pipelines.morning_brief.timeout_s")
    def get_intraday_news(self, date: str, limit: int = 8,
                           collect_if_empty: bool = True,
                           llm_service=None) -> list[dict]:
        """DL-08: 盘中/当日新闻读 API — 收编报告层 NewsCollector 直连 (F-10⑦⑧)。

        先读当日已采集新闻; 为空且 collect_if_empty 时触发一次采集再读。
        NewsCollector 保留在 data_layer, 但消费者只经本方法访问 (D201)。
        """
        from data_layer.news_collector import NewsCollector
        collector = NewsCollector(db=self.db, llm_service=llm_service)
        items = collector.get_news(date, limit=limit)
        if not items and collect_if_empty:
            try:
                collector.collect(date)
                items = collector.get_news(date, limit=limit)
            except Exception as e:
                logger.warning(f"get_intraday_news collect failed: {e}")
        return items or []

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=10)
    def get_history(self, code: str, end_date: str, window: int):
        """返回某只股票的历史数据窗口"""
        return self.loader.get_history(code, end_date, window)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=10)
    def snapshot_range(self, start_date: str, end_date: str):
        """返回日期范围内的所有数据"""
        return self.loader.snapshot_range(start_date, end_date)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=5)
    def forward_return(self, code: str, date: str, horizon: int = 5) -> float | None:
        """获取某只股票的未来N日收益率 (百分点; 未兑现返回 None 不造假 0)"""
        return self.loader.forward_return(code, date, horizon)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=5)
    def realized_return(self, code: str, date: str, horizon: int = 5) -> float | None:
        """截至 date 已实现的 horizon 日收益 (百分点; 选股唯一可用口径, 见 loader 同名方法)"""
        return self.loader.realized_return(code, date, horizon)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=15)
    def get_fwd_returns(self, date: str, horizon: int = 5) -> dict:
        """DL-24: 某日全市场前向收益截面 {code: 收益率(百分点)} — 统一评估读 API"""
        return self.loader.fwd_returns_cross_section(date, horizon)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=120)
    def backfill_fwd_returns(self, target_date: str, horizons: tuple = (1, 5, 20)) -> dict:
        """DL-24: 为指定日期回填前向收益 (每日调度入口, 幂等, 仅写 NULL 行)"""
        return self.loader.backfill_fwd_returns(target_date, horizons)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=5)
    def pit_query(self, query: PITQuery) -> PITResult:
        """Point-in-Time 数据查询"""
        return self._pit.query(query)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=5)
    def pit_financial(self, code: str, date: str) -> PITResult:
        """PIT查询财务数据"""
        return self._pit.query_financial(code, date)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=5)
    def pit_valuation(self, code: str, date: str) -> PITResult:
        """PIT查询估值数据"""
        return self._pit.query_valuation(code, date)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=30)
    def get_concept_coverage(self, start_date: str, end_date: str) -> tuple[float, list[str]]:
        """窗口内 concept_daily 覆盖率 (卡#44 wfo 预检)。

        交易日历口径 = daily_klines 有行日 (多空双查: 库缺行与非交易日都
        不会误报); concept 口径 = concept_daily 有行日。返回
        (coverage, missing_dates) — coverage<1.0 时 missing 列缺失交易日,
        宁可缺失日期可观测, 不静默返空 (铁律#12; 宪法「宁可 NULL 不可
        假值」: 缺日让 get_concept_data 逐股返空表 = 零填充假值直达因子)。
        """
        conn = self.db.get_connection()
        trading = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_klines "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            (start_date, end_date))]
        if not trading:
            return 1.0, []
        # R4-① (09-01 concept 断电案, 统筹 sm-39c8b41402a748ee): covered 分子
        # 加 source 真源口径 — industry_fallback 行不再计入覆盖。断电案实证
        # 08-28/31 两日 5,892 行 fallback 让本守卫 coverage=100% 假绿
        # (walk_forward 带恒值概念因子出报告)。真源 = zzshare 前缀 (含
        # _backfill 后缀, R3 回补数据是合法覆盖); "concept" 票据源
        # (ConceptPipeline limit_up 路径) 保留兼容。
        covered = {r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM concept_daily "
            "WHERE date >= ? AND date <= ? "
            "AND (source LIKE 'zzshare%' OR source = 'concept')",
            (start_date, end_date))}
        missing = [d for d in trading if d not in covered]
        coverage = (len(trading) - len(missing)) / len(trading)
        return coverage, missing

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=5)
    def get_concept_data(self, code: str, date: str) -> list:
        return self.loader.get_concept_data(code, date)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=5)
    def get_lhb_data(self, code: str, date: str, limit: int = 10) -> list:
        return self.loader.get_lhb_data(code, date, limit)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=5)
    def get_analyst_forecasts(self, code: str, date: str, limit: int = 5) -> list:
        return self.loader.get_analyst_forecasts(code, date, limit)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=3)
    def get_sector(self, code: str) -> str:
        return self.loader.get_sector(code)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=3)
    def get_stock_info(self, code: str) -> StockInfo | None:
        return self.loader.get_stock_info(code)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=5)
    def get_sentiment_data(self, date: str) -> SentimentData | None:
        return self.loader.get_sentiment_data(date)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=3)
    def is_st_stock(self, code: str) -> bool:
        return self.loader.is_st_stock(code)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=10)
    def get_risky_codes(self) -> set[str] | None:
        """ST/退市/停牌风险码集合 (批量一次查询)。

        DL-04 fail-closed: 异常/超时/查询失败返回 None (区别于空集),
        调用方收到 None 必须中止本轮选股并告警, 不得拿空集放行。
        """
        return self.loader.get_risky_codes()

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=3)
    def is_suspended(self, code: str, date: str) -> bool:
        return self.loader.is_suspended(code, date)

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=10)
    def check_readiness(self, date: str):
        return self.loader.check_readiness(date)

    @catch_quantstar_error(layer_name="data")
    def compute_fwd_returns(
        self, label_column: str = "fwd_return_20d", horizon: int = 20,
        timeout_s: int | None = None,
    ):
        """计算前向收益率到 factor_daily 表（#110 预算参数化）

        #110: 拆除外层静态 30s 超时装饰器，预算改为参数化（None 回落 30，
        存量无参调用零变）。病灶：09-02 23:46 实跑 67s 超预算（TEMP TABLE
        LEAD 窗口全表扫，规模无关），超时返 None → 上游 TypeError。预算
        真源 = ops.pipelines.model_retrain.data_leg_compute_timeout_s。
        """
        if not validate_identifier(label_column, "column"):
            raise ValueError(f"Invalid column name: {label_column!r}")
        return with_timeout(timeout_s=int(timeout_s or 30))(
            self._compute_fwd_returns_impl
        )(label_column, horizon)

    def _compute_fwd_returns_impl(self, label_column: str, horizon: int) -> None:
        """#110: compute_fwd_returns 实现体（原主体拆出，SQL 未改动）。"""
        if not validate_identifier(label_column, "column"):
            raise ValueError(f"Invalid column name: {label_column!r}")
        conn = self.db.get_connection()
        conn.execute(f"""
            CREATE TEMP TABLE IF NOT EXISTS temp_fwd AS
            SELECT code, date,
                   (LEAD(close, {horizon}) OVER (PARTITION BY code ORDER BY date) / close - 1) * 100 AS fwd_val
            FROM daily_klines
            WHERE close IS NOT NULL AND close > 0
        """)
        conn.execute(f"""
            UPDATE factor_daily
            SET {label_column} = temp_fwd.fwd_val
            FROM temp_fwd
            WHERE factor_daily.code = temp_fwd.code
              AND factor_daily.date = temp_fwd.date
              AND factor_daily.{label_column} IS NULL
        """)
        conn.execute("DROP TABLE IF EXISTS temp_fwd")
        conn.commit()

    @catch_quantstar_error(layer_name="data")
    def get_training_dataset(
        self, label_column: str = "fwd_return_20d",
        timeout_s: int | None = None,
    ) -> list[tuple]:
        """获取 ML 训练数据集（#110 预算参数化，同 compute_fwd_returns 修法）

        #110: 拆除外层静态 30s 超时装饰器。预算真源 =
        ops.pipelines.model_retrain.data_leg_training_timeout_s。
        """
        if not validate_identifier(label_column, "column"):
            raise ValueError(f"Invalid column name: {label_column!r}")
        return with_timeout(timeout_s=int(timeout_s or 30))(
            self._get_training_dataset_impl
        )(label_column)

    def _get_training_dataset_impl(self, label_column: str) -> list[tuple]:
        """#110: get_training_dataset 实现体（原主体拆出，查询未改动）。"""
        if not validate_identifier(label_column, "column"):
            raise ValueError(f"Invalid column name: {label_column!r}")
        conn = self.db.get_connection()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(factor_daily)").fetchall()]
        if "factor_scores" in cols:
            query = f"""
                SELECT f.date, f.code, f.factor_scores, f.{label_column}
                FROM factor_daily f
                WHERE f.{label_column} IS NOT NULL
                  AND f.factor_scores IS NOT NULL
                ORDER BY f.date, f.code
            """
        elif "factor_scores" not in cols:
            # 判例#137 死代码标注: fallback_cols 兜底的旧 schema 兼容层——
            #   当 factor_scores 列不存在时(远旧 schema)才回退列出 19 个裸因子列.
            #   实测: factor_scores 列自 v10 起 100% 存在(投产 100%+),
            #   所以这个分支**在生产不可达**(判例「死代码不删只注」).
            fallback_cols = [
                "pe_ttm", "pb", "roe", "gross_margin", "net_margin",
                "rev_growth", "profit_growth", "momentum_5d",
                "volatility_20d", "volume_ratio", "macd_hist", "rsi_14",
                "pe_rank", "pb_rank", "roe_rank", "momentum_rank", "macd_rank",
                "total_mv", "circ_mv", "concept_count",
            ]
            feat_cols_str = ", ".join(f"f.{c}" for c in fallback_cols)
            query = f"""
                SELECT f.date, f.code, {feat_cols_str}, f.{label_column}
                FROM factor_daily f
                WHERE f.{label_column} IS NOT NULL
                ORDER BY f.date, f.code
            """
        import sqlite3
        old_factory = conn.row_factory
        conn.row_factory = None
        rows = conn.execute(query).fetchall()
        conn.row_factory = old_factory
        return rows

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=5)
    def update_factor_daily(self, code: str, date: str, factor_scores: dict, rank: int = 0, quantile: int = 0):
        """更新 factor_daily 的 factor_scores 字段"""
        import json
        conn = self.db.get_connection()
        cur = conn.execute(
            "UPDATE factor_daily SET factor_scores = ? WHERE code = ? AND date = ?",
            (json.dumps(factor_scores), code, date),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT OR IGNORE INTO factor_daily (code, date, factor_scores) VALUES (?, ?, ?)",
                (code, date, json.dumps(factor_scores)),
            )
        conn.commit()

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=30)
    def persist_model_predictions(self, rows: list[tuple]):
        """批量写入 model_predictions (date, code, ml_score, factor_score, blended_score, model_version)"""
        if not rows:
            return
        conn = self.db.get_connection()
        conn.executemany(
            "INSERT OR REPLACE INTO model_predictions "
            "(date, code, ml_score, factor_score, blended_score, model_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    @staticmethod
    def _fiscal_health_check(table: str, latest_str: str, audit_date: str,
                             is_holiday: bool, calendar_error: str | None) -> tuple:
        """卡2 季表判据 (readiness-announce-aware-unify, 2026-10-03)。

        纯函数面 (供单元钉): 季表窗口判与 freshness_audit._fiscal_window_ok
        同源 (三头合一, 不造第二套)。
        返回 (status, detail, metric):
          假日 → pass + 'holiday (non-trading day)' 标注 (可观测性硬要求:
                 假日判绿与源全挂判绿在观测上不可区分, 不标注=判例 #147)
          日历故障 fail-open → pass + 'calendar_fail_open' 标注 (判例 #147
                 第三形态「故障放行恒真」的反向钉: 故障放行必须可见)
          窗内 → pass + stale 显形 (卡3, 判例 #128: 只问有行不问多旧
                 = 把陈数据判健康, 陈值必须可见)
          窗外 → warn + 真断血候选显形 (三头同色: delayed→warn)
        """
        stale = fiscal_stale_days(audit_date, latest_str)
        metric = float(stale if stale is not None else 0)
        if is_holiday:
            return ("pass",
                    f"{table} holiday (non-trading day) — 假日判绿, "
                    f"max_date={latest_str}, stale={stale}d", metric)
        if calendar_error:
            return ("pass",
                    f"{table} calendar_fail_open ({calendar_error}) — 日历故障放行"
                    f", max_date={latest_str}, stale={stale}d", metric)
        if fiscal_window_ok(audit_date, latest_str):
            return ("pass",
                    f"{table} 季表披露窗内: max_date={latest_str}, "
                    f"stale={stale}d (窗口判=freshness_audit 同源)", metric)
        return ("warn",
                f"{table} 披露窗已过: max_date={latest_str}, "
                f"stale={stale}d — 新报告期未到 (真断血候选)", metric)

    def health(self):
        from datetime import datetime

        from common.utils import bj_date
        from ops_layer.core.models import HealthCheck, LayerHealth
        checks = []

        # 1. Database connectivity
        try:
            conn = self.db.get_connection()
            conn.execute("SELECT 1").fetchone()
            checks.append(HealthCheck(name="database", status="pass", detail="数据库连接正常"))
        except Exception as e:
            checks.append(HealthCheck(name="database", status="fail", detail=f"数据库连接失败: {e}"))
            return LayerHealth(layer="data", status="red", checks=checks, summary="数据库连接失败")

        # 2. Per-table data freshness (multi-table, independent thresholds)
        # (table, green<=d, yellow<=d, red>d)
        thresholds = [
            ("daily_klines", 1, 3, 5),
            ("factor_daily", 1, 3, 5),
            ("model_predictions", 1, 3, 5),
            ("stock_valuation", 1, 3, 5),
            ("stock_moneyflow", 1, 3, 7),
            ("stock_limit_up_down", 1, 3, 7),
            ("lhb_daily", 1, 3, 7),
            ("concept_daily", 1, 3, 7),
            # alpha_factors 为遗留审计缓存 (无实时写入方, S3 豁免), 实时管线消费
            # factor_daily.factor_scores (上方 1/3/5 严格探针)。不纳入新鲜度探针,
            # 否则永久 red 且触发 F6.3 health_red 误告警。
            ("analyst_forecasts", 3, 7, 14),
            ("stock_financials", 7, 14, 30),
            ("market_sentiment", 1, 3, 7),
        ]
        today = datetime.strptime(bj_date(), "%Y-%m-%d").date()
        # 卡2 (readiness-announce-aware-unify, 2026-10-03): 假日不红。
        # TradingCalendar fail-open: 日历故障放行但 detail 留痕 (calendar_fail_open,
        # 判例 #147 第三形态反向钉 — 故障放行与假日判绿必须可区分)。
        calendar_error: str | None = None
        try:
            is_holiday = not TradingCalendar(self.db).is_trading_day(bj_date())
        except Exception as _ce:  # noqa: BLE001 — 日历故障不炸健康面
            is_holiday = False
            calendar_error = f"{type(_ce).__name__}: {_ce}"
        worst = "pass"
        for table, g, y, r in thresholds:
            try:
                row = conn.execute(f"SELECT MAX(date) FROM {table}").fetchone()
                latest_str = row[0] if row and row[0] else ""
                if not latest_str:
                    checks.append(HealthCheck(name=f"freshness_{table}", status="fail",
                        detail=f"{table} 表无数据", metric=9999.0, threshold=float(r)))
                    worst = "fail"
                    continue
                try:
                    latest_date = datetime.strptime(latest_str, "%Y-%m-%d").date()
                except ValueError:
                    checks.append(HealthCheck(name=f"freshness_{table}", status="warn",
                        detail=f"{table} 日期格式异常: {latest_str}", metric=9999.0, threshold=float(r)))
                    if worst != "fail":
                        worst = "warn"
                    continue
                days_diff = (today - latest_date).days
                # 卡2+卡3: 季表分流 (FISCAL_TABLES) — 表级距今差对季报表恒假警报
                # (09-05 卡 131040min 实证); 判据切窗口 = freshness_audit 同源。
                # 日更表假日面照 pass + 标注 (可观测性硬要求)。
                if table in DataReadiness.FISCAL_TABLES:
                    # 季表: 窗口判独立于假日 (窗口宽 45d 天然吸收假日 —
                    # 06-30 对 10-04 的 stale=95d 是季度语义非陈旧)。
                    s, detail, metric = self._fiscal_health_check(
                        table, latest_str, bj_date(), is_holiday, calendar_error)
                    checks.append(HealthCheck(name=f"freshness_{table}", status=s,
                        detail=detail, metric=metric, threshold=float(r)))
                    if s == "warn" and worst != "fail":
                        worst = "warn"
                    continue
                if is_holiday or calendar_error:
                    # 日更表假日面: 假日语义 v3 (s2 假红逼出) — 假日不豁免
                    # 「真陈旧」: 数据停上一交易日=源侧当日未到 (pass 合理);
                    # 停在上一交易日之前 (days_diff_rel > g) = 真陈旧, 假日
                    # 照走原三色判定 (真断血假日也显形, 判例 #147 不成全能豁免)。
                    # v4: 上一交易日 = 日历回溯 (不从库内 klines 锚 —
                    # 库停更=病源, 拿它当锚=用有病产物当基线, 判例 #147 复活面)。
                    _prev_trade = None
                    try:
                        from data_layer.health.readiness import (
                            prev_trading_day_before as _ptdb,
                        )
                        _prev_trade = _ptdb(bj_date())
                    except Exception:
                        _prev_trade = None
                    if _prev_trade and latest_date < datetime.strptime(
                            _prev_trade, "%Y-%m-%d").date():
                        days_diff_rel = (datetime.strptime(
                            _prev_trade, "%Y-%m-%d").date() - latest_date).days
                        if days_diff_rel > g:
                            s = "warn" if days_diff_rel <= y else "fail"
                            detail = (f"{table} holiday 但陈旧: 最新 {latest_str} "
                                      f"停在上一交易日 {_prev_trade} 之前 {days_diff_rel}d "
                                      f"(阈值 g{g}/y{y})")
                            checks.append(HealthCheck(name=f"freshness_{table}", status=s,
                                detail=detail, metric=float(days_diff_rel),
                                threshold=float(r)))
                            if s == "fail":
                                worst = "fail"
                            elif s == "warn" and worst != "fail":
                                worst = "warn"
                            continue
                    s, detail, metric = self._fiscal_health_check(
                        table, latest_str, bj_date(), is_holiday, calendar_error)
                    checks.append(HealthCheck(name=f"freshness_{table}", status=s,
                        detail=detail, metric=metric, threshold=float(r)))
                    if s == "warn" and worst != "fail":
                        worst = "warn"
                    continue
                # 三色判定: green<=g, yellow 在 (g, y], red > y。
                # fail 分界取黄阈值 y (更严格): 日更管线合法最大陈旧 = 周一早 3 天
                # (上一交易日=周五), 仍为 yellow; 4 天起即 red, 可捕获真实断档。
                # (若取 r 作分界, lhb_daily 08-14 断档 4 天会被掩盖为 yellow。)
                # 第三阈值 r 仅作 detail 展示参考, 不参与判定。
                if days_diff <= g:
                    s = "pass"
                elif days_diff <= y:
                    s = "warn"
                else:
                    s = "fail"
                checks.append(HealthCheck(name=f"freshness_{table}", status=s,
                    detail=f"{table} 最新: {latest_str} (距今{days_diff}天, 阈值 g{g}/y{y}/r{r})",
                    metric=float(days_diff), threshold=float(r)))
                if s == "fail":
                    worst = "fail"
                elif s == "warn" and worst != "fail":
                    worst = "warn"
            except Exception as e:
                checks.append(HealthCheck(name=f"freshness_{table}", status="warn",
                    detail=f"{table} 新鲜度检查失败: {e}", metric=9999.0, threshold=float(r)))
                if worst != "fail":
                    worst = "warn"

        status = "green" if worst == "pass" else "red" if worst == "fail" else "yellow"
        return LayerHealth(layer="data", status=status, checks=checks,
            summary=f"Data Layer {'正常' if status == 'green' else '需关注'} ({worst})")

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=10)
    def get_quality_checks(self, date: str) -> list[dict]:
        """DL-11: 读 quality_checks 表 (DataQuality 接线产物)。

        返回当日质量检查记录 [{date, table_name, check_name, level, result,
        detail, created_at}]; 表不存在 (管线未首跑) 返回空不抛错。
        """
        conn = self.db.get_connection()
        try:
            rows = conn.execute(
                "SELECT date, table_name, check_name, level, result, detail, "
                "created_at FROM quality_checks WHERE date = ? "
                "ORDER BY level DESC, check_name", (date,),
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=10)
    def get_freshness_audit(self, date: str) -> list[dict]:
        """DL-12: 读 freshness_audit 表 (DataReadiness 接线产物)。

        返回当日新鲜度审计记录 [{date, table_name, expected_by, actual_at,
        delay_min, status, created_at}]; 表不存在 (管线未首跑) 返回空不抛错。
        """
        conn = self.db.get_connection()
        try:
            rows = conn.execute(
                "SELECT date, table_name, expected_by, actual_at, delay_min, "
                "status, created_at FROM freshness_audit WHERE date = ? "
                "ORDER BY status DESC, table_name", (date,),
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=30)
    def get_source_health(self) -> dict:
        """DL-11: SourceHealthChecker 接线 — 主动探测全部注册源健康。

        返回 {source_name: {status, latency_s, last_check, capabilities}};
        ops 定时任务 (每 30 分钟) 调用本方法, 不健康源告警 (Q013)。
        """
        from data_layer.source_health import SourceHealthChecker
        checker = SourceHealthChecker(self._source_registry)
        return checker.check_all()

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=60)
    def get_integrity_summary(self) -> dict:
        """DL-11: DataIntegrityChecker 接线 — 关键表行数/日期范围/异常汇总。

        返回 {total_tables, ok, warning, error, error_tables, warning_tables,
        tables}; 供周度运维任务消费。
        """
        from data_layer.integrity import DataIntegrityChecker
        checker = DataIntegrityChecker(self.db)
        results = checker.check_all()
        summary = checker.summary(results)
        summary["tables"] = results
        return summary

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=10)
    def get_index_daily(self, code: str, start_date: str = "", end_date: str = "") -> list[dict]:
        """DL-23: 读指数日线 (供回测基准对比 BT-04 / 报告大盘段落消费)。

        返回 [{date, open, high, low, close, volume, name}] 按日期升序;
        表不存在 (管线未首跑) 返回空不抛错。
        """
        conn = self.db.get_connection()
        try:
            sql = ("SELECT date, open, high, low, close, volume, name "
                   "FROM index_daily WHERE code = ?")
            params: list = [code]
            if start_date:
                sql += " AND date >= ?"
                params.append(start_date)
            if end_date:
                sql += " AND date <= ?"
                params.append(end_date)
            sql += " ORDER BY date"
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    @catch_quantstar_error(layer_name="data")
    @with_timeout(timeout_s=10)
    def get_stock_news(self, code: str, date: str, limit: int = 20) -> list[dict]:
        """DL-20: 读个股结构化新闻+情绪分 (策略层 deep_review ST-06 消费, 替代直连 akshare)。

        返回 [{code, date, title, source, sentiment, sentiment_score, summary}]:
        个股新闻 (code 精确匹配) 在前, 当日市场级新闻 (code='') 补足在后,
        各组按日期降序; 表不存在或无数据返回空列表不抛错。
        """
        conn = self.db.get_connection()
        try:
            rows = conn.execute(
                "SELECT code, date, title, source, sentiment, sentiment_score, summary "
                "FROM stock_news WHERE date LIKE ? AND (code = ? OR code = '') "
                "ORDER BY CASE WHEN code = ? THEN 0 ELSE 1 END, date DESC LIMIT ?",
                (date + "%", code, code, limit),
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    @catch_quantstar_error(layer_name="data", fallback_return=[])
    @with_timeout(timeout_s=10, fallback_return=[])
    def get_stock_news_live(self, code: str, limit: int = 10) -> list[dict]:
        """DL-71: 现场按股拉取个股新闻 (akshare stock_news_em), 按需取不落库。

        背景 (DL-66 普查): stock_news 表三个采集源全是宏观新闻流, code 恒空串,
        get_stock_news(code,...) 个股分支恒返回空 → 深度研判在新闻维度一直瞎。
        统筹裁定 (sm-b3254cb457254ab1): 不做每日全量采集 (5500 股×日 塞爆 ddp 预算),
        改按需取 —— 仅对候选股现场拉, 调用量从 5500/天 降到几十/天, 时效更好。

        策略层经本读 API 取数 (DL-41 边界: strategy_layer 不直连 akshare)。
        失败/超时诚实返回 [] (不造假值, 不阻断研判)。
        """
        try:
            import akshare as ak
            df = ak.stock_news_em(symbol=code)
        except Exception:
            return []
        if df is None or df.empty:
            return []
        items: list[dict] = []
        for _, r in df.head(limit).iterrows():
            title = str(r.get("新闻标题", "") or "").strip()
            if not title:
                continue
            items.append({
                "code": code,
                "date": str(r.get("发布时间", "") or "")[:10],
                "title": title,
                "source": str(r.get("文章来源", "") or "eastmoney"),
                "sentiment": "neutral",
                "sentiment_score": 50.0,
                "summary": str(r.get("新闻内容", "") or "")[:200],
            })
        return items

    def shutdown(self, timeout: float = 10.0):
        if self.db:
            self.db.close()
        return True

    def get_float_shares(self, date: str) -> dict[str, float]:
        """DL-50: 按日全市场流通股本读 API。

        返回 {code: float_shares(股)}。数据源: tushare daily_basic.float_share。
        不写库 (D-66 已弃用 circ_mv 物理列), 仅供因子计算按需调用。
        源不可用时返回空 dict (诚实降级, 不造假数据)。
        """
        sources = self._source_registry.get_sources_for("float_share")
        for source in sources:
            try:
                result = source.get_data("float_share", {"date": date})
                if result:
                    return result
            except Exception:  # noqa: BLE001
                continue
        return {}

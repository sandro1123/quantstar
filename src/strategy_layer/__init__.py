# ruff: noqa: F401
"""Strategy Layer — 因子引擎 + 聚合 + 选股 + 信号 + Agent审核 + 盘中监控"""
import json
from dataclasses import dataclass, field
from datetime import datetime

from common.decorators import catch_quantstar_error, with_timeout
from common.logging import get_logger
from common.utils import generate_trace_id
from strategy_layer._models import DailyRunResult, FactorScores, PostMarketResult, TradeSignals
from strategy_layer.agent_gate.deep_review import DeepReview
from strategy_layer.aggregation.aggregator import DEFAULT_WEIGHTS, Aggregator, FactorDaily
from strategy_layer.engine import CleanedFactorMatrix, FactorEngine
from strategy_layer.factors import ALL_FACTORS
from strategy_layer.intraday import (
    IntradayAlert,
    IntradayStrategyMonitor,
    PreMarketBrief,
    analyze_minute_data,
)
from strategy_layer.selection.selector import SelectionPipeline, SelectionResult
from strategy_layer.signals.generator import SignalGenerator, TradeSignal

logger = get_logger("strategy")

# P0-5 写入侧工具 (派单 sm-2d7d72e0e5244dfa Day 2 卡 2): verdict 注入 reason 的单点。
# 三闸 (P0-2-TDD-SPEC):
#   - 闸1 死区 WAR: verdict 空 → log.warning 显形 (第十变体判死区),不可静默
#   - 闸2 闸拒 set: verdict ∈ REJECT_VERDICTS → 不拼接
#   - 闸3 钢钉一: verdict='推荐' + confidence 高 → 正确拼接前缀
_PICK_REASON_REJECT = {"不推荐", "观望", "警惕", "卖出", "规避"}


def _pick_reason_part(agent, sig):
    """P0-5 五条线连通: agent verdict 写进 signal.reason (single entry point).

    闸拒: verdict ∈ _PICK_REASON_REJECT → 不拼接 (「观望」不应混入「推荐」语义)。
    显形: verdict 空/缺 → log.warning (第十变体判死区),不可静默。
    """
    import logging
    logger = logging.getLogger(__name__)
    if agent is None:
        return
    verdict = (getattr(agent, "verdict", None) or "").strip()
    conf = getattr(agent, "confidence", 0.0) or 0.0
    if not verdict:
        logger.warning(
            "P0-5 verdict 字段缺 (第十变体判死区): code=%s conf=%.2f — "
            "跳过拼接,但需查 deep_review 是否真填值或 dataclass 是否漏字段",
            getattr(sig, "code", "?"), conf)
        return
    if verdict in _PICK_REASON_REJECT:
        return
    sig.reason = (
        f"agent 推荐（verdict={verdict}, confidence={conf:.2f}）"
        f" | {sig.reason}"
    )



@dataclass
class StrategyStageContext:
    """ST-14: 阶段间传递的类型化上下文 (取代局部变量), 支持单阶段独立重跑"""
    date: str
    mode: str = "live"
    current_positions: dict | None = None
    codes: list[str] = field(default_factory=list)
    matrix: object = None
    used_weights: dict | None = None
    factor_daily: list = field(default_factory=list)
    regime: str = "neutral"
    ml_model_version: str = ""
    selection: object = None
    agent_gate_enabled: bool = True
    agent_reviews: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    run_id: str = ""           # #79 run 级溯源: 父运行标识 (strategy_runs.run_id)
    prov_tracker: object = None  # #79: ProvenanceTracker (埋点期存活; ST-14 类型化字段)
    prov_t0: object = None       # #79: run 起点 datetime


# ST-14: 阶段依赖声明 (requires/produces), 供 pipeline runner 校验 (G41-G42 接线前置)
STAGE_SPECS = [
    {"name": "factor_compute", "requires": ["codes"], "produces": ["matrix"]},
    {"name": "aggregate", "requires": ["matrix"], "produces": ["factor_daily", "used_weights"]},
    {"name": "ml_score", "requires": ["factor_daily"], "produces": ["regime", "ml_model_version"]},
    {"name": "select", "requires": ["factor_daily", "regime"], "produces": ["selection"]},
    {"name": "agent_gate", "requires": ["selection"], "produces": ["agent_reviews"]},
    {"name": "signal_gen", "requires": ["selection", "factor_daily", "regime"], "produces": ["signals"]},
]


class StrategyLayer:
    def __init__(self, config=None, data_layer=None, agent_layer=None, db=None, mode: str = "live", trace_id: str | None = None):
        self._trace_id = trace_id or generate_trace_id()
        self.config = config
        self.data_layer = data_layer
        self.agent_layer = agent_layer
        self.db = db or (getattr(data_layer, 'db', None) if data_layer else None)
        self.mode = mode
        self.factor_engine = FactorEngine(data_layer, mode=mode, db=self.db)
        self.aggregator = Aggregator()
        self.selector = SelectionPipeline(config=self.config, data_layer=data_layer)
        self.signal_generator = SignalGenerator(config=self.config)
        self.deep_review = DeepReview(agent_layer, data_layer=data_layer)  # DL-41
        from agent_layer.debate.experts import ALL_EXPERTS
        self.deep_review.experts = ALL_EXPERTS
        self.intraday_monitor = IntradayStrategyMonitor(factor_engine=self.factor_engine, data_layer=data_layer)
        self._state = {}

    def _ensure_tables(self):
        if self.db:
            try:
                from strategy_layer.tables import init_strategy_tables
                conn = self.db.get_connection()
                init_strategy_tables(conn)
            except Exception as e:
                logger.debug(f"Strategy tables init: {e}")

    def start(self):
        self._ensure_tables()
        logger.info("Strategy Layer 启动完成")

    def health(self):
        from datetime import datetime, timedelta

        from common.utils import bj_date
        from ops_layer.core.models import HealthCheck, LayerHealth
        checks = []

        db = getattr(self.data_layer, "db", None) if self.data_layer else None

        # 1. Factor daily freshness
        factor_latest = None
        if db:
            try:
                conn = db.get_connection()
                row = conn.execute("SELECT MAX(date) FROM factor_daily").fetchone()
                factor_latest = row[0] if row and row[0] else None
                if factor_latest:
                    latest_date = datetime.strptime(factor_latest, "%Y-%m-%d").date()
                    today = datetime.strptime(bj_date(), "%Y-%m-%d").date()
                    days_diff = (today - latest_date).days
                    fs_status = "pass" if days_diff <= 3 else ("warn" if days_diff <= 5 else "fail")
                    checks.append(HealthCheck(name="factor_freshness", status=fs_status,
                        detail=f"因子最新日期: {factor_latest} (距今{days_diff}天)",
                        metric=float(days_diff), threshold=5.0))
                else:
                    checks.append(HealthCheck(name="factor_freshness", status="fail", detail="factor_daily 表无数据"))
            except Exception as e:
                checks.append(HealthCheck(name="factor_freshness", status="fail", detail=f"因子新鲜度检查失败: {e}"))

            # 2. Factor count for latest date
            if factor_latest:
                try:
                    row = conn.execute("SELECT COUNT(*) FROM factor_daily WHERE date = ?", (factor_latest,)).fetchone()
                    count = row[0] if row else 0
                    fc_status = "pass" if count > 100 else ("warn" if count > 0 else "fail")
                    checks.append(HealthCheck(name="factor_count", status=fc_status,
                        detail=f"因子数据: {count} 条记录 ({factor_latest})",
                        metric=float(count), threshold=100.0))
                except Exception as e:
                    checks.append(HealthCheck(name="factor_count", status="fail", detail=f"因子计数失败: {e}"))

        else:
            checks.append(HealthCheck(name="database", status="warn", detail="数据层未连接"))

        # 3. Factor engine readiness
        try:
            factor_eng = getattr(self, "factor_engine", None)
            engine_factors = getattr(factor_eng, "factors", None)
            if engine_factors is not None:
                factor_count = len(engine_factors)
            else:
                factor_count = len(factor_eng.ALL_FACTORS) if factor_eng and hasattr(factor_eng, "ALL_FACTORS") else 0
            fe_status = "pass" if factor_count > 0 else "warn"
            checks.append(HealthCheck(name="factor_engine", status=fe_status,
                detail=f"因子引擎就绪 ({factor_count} 个因子)"))
        except Exception as e:
            checks.append(HealthCheck(name="factor_engine", status="fail", detail=f"因子引擎异常: {e}"))

        # Overall
        status = "green"
        for c in checks:
            if c.status == "fail":
                status = "red"
                break
            elif c.status == "warn":
                status = "yellow"
        return LayerHealth(layer="strategy", status=status, checks=checks,
            summary=f"Strategy Layer {'正常' if status == 'green' else '需关注'}")

    def shutdown(self, timeout: float = 10.0):
        self._state = {}
        logger.info("Strategy Layer 状态已保存")
        return True

    @catch_quantstar_error(layer_name="strategy")
    @with_timeout(timeout_s=14400)
    def run_daily(self, date: str, mode: str | None = None, current_positions: dict | None = None) -> DailyRunResult:
        """每日策略运行 (ST-14: 薄编排层, 各阶段独立可重跑)。

        阶段链: factor_compute → aggregate → ml_score → select → agent_gate → signal_gen。
        阶段间以 StrategyStageContext 类型化对象传递 (取代局部变量), 依赖声明见 STAGE_SPECS。
        对外签名不变, ops 调用方无感; 行为与拆分前一致 (对拍测试守护)。
        """
        ctx = StrategyStageContext(date=date, mode=mode or self.mode, current_positions=current_positions)
        self._run_prov_start(ctx)  # #79: run 级溯源登记
        logger.info(f"Strategy.run_daily({date}) mode={ctx.mode}")

        if ctx.mode == "backtest":
            logger.warning("backtest_live_gap — 使用全部可用数据，无速率限制，需注意滑点与成交率假设")
        elif ctx.mode == "paper":
            logger.info("paper mode — 模拟交易，额外日志已启用")

        # #79: 全段包裹 — 异常路径 finish_run('failed') 后 re-raise 原异常 (验收红线)。
        # 裸函数面原异常穿透; 装饰面 catch_quantstar_error 照旧转 None — 对外行为零改动。
        # 缩进 +4 纯机械, 无语义变化; ST-14 薄编排守卫由 helper 抽取保住。
        try:
            # 0. 更新引擎模式
            self.factor_engine.set_mode(ctx.mode)

            # 1. 获取股票列表
            ctx.codes = self._get_stock_codes(date)
            if not ctx.codes:
                logger.warning(f"No stocks found for {date}")
                # W1 no-stocks-obs: no_stocks 观测键随 results_json 落库
                # (假日早退 vs 源挂早退区分, 机理见 _no_stocks_observation)
                self._run_prov_finish(ctx, "no_stocks", 0, 0,
                                      results=self._no_stocks_observation(date))
                return DailyRunResult(date=date, signals=[], selection=None, regime="neutral", factor_count=0, stock_count=0)

            self._stage_factor_compute(ctx)
            self._stage_aggregate(ctx)
            self._stage_ml_score(ctx)
            if self._stage_select(ctx):  # fail-closed 中止
                self._run_prov_finish(ctx, "select_failclosed", len(ctx.codes), 0)
                return DailyRunResult(
                    date=date, signals=[], selection=None, regime=ctx.regime,
                    factor_count=len(ALL_FACTORS), stock_count=len(ctx.codes),
                )
            self._stage_agent_gate(ctx)
            self._stage_signal_gen(ctx)

            self._run_prov_finish(ctx, "success", len(ctx.codes), len(ctx.signals),
                                  results={"signals": [getattr(s, "code", "") for s in ctx.signals]})
            return DailyRunResult(
                date=date,
                signals=ctx.signals,
                selection=ctx.selection,
                regime=ctx.regime,
                factor_count=len(ALL_FACTORS),
                stock_count=len(ctx.codes),
            )
        except Exception:  # noqa: E722 — 裸 re-raise 保原异常
            self._run_prov_finish(ctx, "failed", len(ctx.codes), len(ctx.signals))
            raise

    # ---- #79 run 级溯源 helpers (跨阶段横切, 非链上 stage; 门控=provenance.enabled,
    #      与 Stage 6 signal 级同闸 — 共享 kill-switch 语义, commit 注明) ----

    def _run_prov_start(self, ctx: StrategyStageContext) -> None:
        """#79: run_daily 入口登记运行; 失败只告警不阻塞主流程。"""
        if not (self.config and self.config.get("strategy.provenance.enabled", False)
                and self.db is not None):
            return
        try:
            from strategy_layer.provenance import ProvenanceTracker
            ctx.prov_tracker = ProvenanceTracker(db=self.db)
            ctx.run_id = ctx.prov_tracker.start_run(
                date=ctx.date, mode=ctx.mode,
                parameters={"factor_count": len(ALL_FACTORS)})
            ctx.prov_t0 = datetime.now()
        except Exception as e:
            logger.warning(f"run provenance start skipped: {e}")

    def _no_stocks_observation(self, date: str) -> dict:
        """no_stocks 观测键 (W1 no-stocks-obs): 当日 kline 真实行数 + 交易日判定。

        两键组合可判早退病因:
          trading_day=True  & kline_rows=0   → 源挂早退 (病: 源/管道断供)
          trading_day=False & kline_rows=0   → 假日早退 (正常: 当天非交易日)
          trading_day=True  & kline_rows>0   → 边界 (kline 有行 codes 仍空)

        计数面读不到 (db 挂) → kline_rows=None + fail_count=True:
        「读不到」≠「真的是 0」— 判例: 空集与零命中必须可区分。
        """
        from common.calendar import TradingCalendar
        try:
            trading_day = TradingCalendar(db=self.db).is_trading_day(date)
        except Exception as cal_e:
            logger.warning(f"no_stocks obs: calendar 判定失败 ({cal_e}) — fail-open 留痕")
            trading_day = None
        try:
            kline_rows = self.data_layer.count_daily_klines(date)
            if not isinstance(kline_rows, int) or isinstance(kline_rows, bool):
                # 健性收编: 计数面返回非 int (Mock/None/异常对象) → 按读不到处理,
                # 不许污染 results_json (json.dumps 炸会连带拖垮 finish_run 整个
                # 早退留痕 — status 卡 running 比缺观测键更假)。判例: 附加观测面
                # 失败不得反噬主留痕 (铁律#12 可观测降级)。
                raise TypeError(f"count_daily_klines 返回非 int: {type(kline_rows).__name__}")
        except Exception as cnt_e:
            logger.warning(f"no_stocks obs: kline 计数失败 ({cnt_e}) — fail_count 留痕非静默 0")
            return {"kline_rows": None, "trading_day": trading_day, "fail_count": True}
        return {"kline_rows": kline_rows, "trading_day": trading_day, "fail_count": False}

    def _run_prov_finish(self, ctx: StrategyStageContext, status: str,
                         stock_count: int, signal_count: int,
                         results: dict | None = None) -> None:
        """#79: 三 return 点 + 异常路径统一出口; finish 失败只告警不阻塞。"""
        if ctx.prov_tracker is None or ctx.run_id == "":
            return
        try:
            ctx.prov_tracker.finish_run(
                ctx.run_id, status=status, stock_count=stock_count,
                signal_count=signal_count,
                duration_s=round((datetime.now() - ctx.prov_t0).total_seconds(), 3) if ctx.prov_t0 else 0.0,
                results=results)
        except Exception as e:
            logger.warning(f"run provenance finish skipped: {e}")

    # ---- ST-14: 阶段方法 (各为独立函数, 可单阶段重跑; 依赖声明见 STAGE_SPECS) ----

    def _stage_factor_compute(self, ctx: StrategyStageContext) -> None:
        """Stage 1: 因子计算 (按 CPU 核数并行) + 低相关性检测。produces: matrix"""
        import os as _os
        _n_workers = max(1, min(_os.cpu_count() or 1, 4))
        if self.config:
            try:
                _cfg_w = int(self.config.get("strategy.factor_workers", 0) or 0)
                if _cfg_w > 0:
                    _n_workers = _cfg_w
            except Exception:
                pass
        ctx.matrix = self.factor_engine.compute_all(ctx.codes, ctx.date, workers=_n_workers)
        logger.info(f"Computed factors for {len(ctx.matrix)} stocks")
        try:
            self.aggregator.detect_redundant_factors(ctx.matrix, threshold=0.7)
        except Exception as e:
            logger.debug(f"Factor correlation skip: {e}")

    def _stage_aggregate(self, ctx: StrategyStageContext) -> None:
        """Stage 2: 权重载入 + 聚合 + forward_return 填充 (仅 IC) + factor_daily 持久化 + IC 观测。
        produces: factor_daily, used_weights"""
        # 载入持久化因子权重 (IC feedback loop) — DB-backed (F3.2)
        from strategy_layer.feedback.persistence import WeightPersistence
        wp = WeightPersistence(config=self.config, db=self.db)
        persisted_weights = wp.load_weights(default_weights=None)
        if not persisted_weights:
            # 铁律#12: 空表回落不可静默 — factor_weights 无数据时显式告警,
            # 提示聚合正运行在出厂专家先验 (DEFAULT_WEIGHTS) 上。
            logger.warning(
                "factor_weights empty -> falling back to DEFAULT_WEIGHTS "
                "(%d factors); awaiting first successful ic_feedback seed",
                len(DEFAULT_WEIGHTS),
            )
        ctx.used_weights = persisted_weights if persisted_weights else None

        ctx.factor_daily = self.aggregator.aggregate(ctx.matrix, weights=ctx.used_weights, use_correlation_filter=True)

        # 填充 forward_return (F3.2 断链1): 从 data_layer 读 T+N 前向收益,
        # 只供下方 IC 观测使用, 绝不进入选股排序 (审计 S-1: 历史回放里是未来信号)。
        # 当日的前向收益通常尚未实现 (为 None), 属正常; 真正的 IC 计算由
        # ops ic_feedback handler 在滞后日执行。
        try:
            self.aggregator.fill_recent_returns(ctx.factor_daily, self.data_layer, ctx.date)
        except Exception as e:
            logger.debug(f"fill_recent_returns skip: {e}")

        # ST-09 拆假货: 原此处无条件跑 MultiTimeframeScorer.score_batch 且周/月分数
        # 就是日分数本身 → 合成结果恒等于日分数, 每日白打一条 degraded warning (审计 S-07)。
        # 假合成段已删除。多时间帧真实现排队 (需周/月频因子计算管道, 决策账本 S-06),
        # 届时以真实周/月分数输入重新接入。

        # 持久化 factor_scores + rank 到数据库 (供 ML 训练)
        try:
            for fd in ctx.factor_daily:
                self.data_layer.update_factor_daily(fd.code, fd.date, fd.factor_scores, rank=getattr(fd, 'rank', 0), quantile=getattr(fd, 'quantile', 0))
        except Exception as e:
            logger.warning(f"Failed to persist factor_scores: {e}")

        # IC feedback: record daily IC for each factor (F3.2)
        # ST-01 止血 (2026-08-18): inline 路径仅 record_ic 观测, 不再写 factor_weights。
        # 原因: forward_return 对最新日的 fallback 恒为 0 (审计 S-01/S-02), IC 衰减据此把
        # 专家权重稀释成近均匀分布——反馈环不是在学习, 而是在遗忘。权重重建与 IC 环修复
        # 明确延后 (三段式: 止血 → record-only ≥20 有效交易日 → 周频+人在回路审批调权,
        # 见决策账本 S-03; 前置: 数据层 forward_return 语义修复 + fwd_return_5d 回填调度)。
        try:
            from strategy_layer.feedback.ic_tuner import ICTuner
            tuner = ICTuner()
            factor_scores_dict = {}
            forward_returns_dict = {}
            for fd in ctx.factor_daily:
                for fname, fval in fd.factor_scores.items():
                    factor_scores_dict.setdefault(fname, {})[fd.code] = fval
                if fd.forward_return is not None:
                    forward_returns_dict[fd.code] = fd.forward_return
            if factor_scores_dict and forward_returns_dict:
                daily_ic = tuner.compute_ic(factor_scores_dict, forward_returns_dict)
                wp.record_ic(daily_ic, sample_size=len(forward_returns_dict))
                logger.info(
                    f"IC feedback: recorded IC for {len(daily_ic)} factors "
                    "(record-only, weight updates halted by ST-01)")
            else:
                logger.info("IC feedback: no forward returns available yet (normal for current date)")
        except Exception as e:
            logger.warning(f"IC feedback skipped: {e}")

    def _persist_daily_regime(self, date: str, regime: str, confidence: float = 1.0,
                              snapshot_metrics: dict | None = None) -> None:
        """ST-15: 幂等落库 daily_regime 表 (date PRIMARY KEY)"""
        if not self.db:
            return
        try:
            self._ensure_tables()
            metrics = snapshot_metrics or {}
            detail_json = json.dumps(metrics, ensure_ascii=False) if metrics else None
            conn = self.db.get_connection()
            conn.execute("""
                INSERT INTO daily_regime (date, regime, confidence, avg_change, breadth, volume_score, composite_score, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    regime=excluded.regime,
                    confidence=excluded.confidence,
                    avg_change=excluded.avg_change,
                    breadth=excluded.breadth,
                    volume_score=excluded.volume_score,
                    composite_score=excluded.composite_score,
                    detail=excluded.detail,
                    created_at=datetime('now', '+8 hours')
            """, (
                date, regime, confidence,
                metrics.get("avg_change"),
                metrics.get("breadth"),
                metrics.get("volume_score"),
                metrics.get("composite_score"),
                detail_json
            ))
            conn.commit()
            logger.info(f"daily_regime persisted: {date} -> {regime}")
        except Exception as e:
            logger.warning(f"Failed to persist daily_regime: {e}")

    def _stage_ml_score(self, ctx: StrategyStageContext) -> None:
        """Stage 3: regime 判定 + ML 评分 + 混合 + model_predictions 持久化 + daily_regime 落库。
        produces: regime, ml_model_version"""
        ctx.regime = self.market_regime(ctx.date)
        metrics = getattr(self, "_last_regime_metrics", None) or {}
        confidence = 1.0 if ctx.regime != "neutral" else 0.5
        self._persist_daily_regime(ctx.date, ctx.regime, confidence=confidence, snapshot_metrics=metrics)

        # ML scoring (RegimeAwareScorer — 环境感知模型切换)
        # 特征对齐：按模型 feature_names 顺序构建向量，否则 88 键 vs 68 特征维度不匹配 → ml_score 恒 0
        try:
            import os
            os.environ.setdefault('OMP_NUM_THREADS', '1')
            from strategy_layer.ml.regime_scorer import build_regime_aware_scorer, score_stocks
            from strategy_layer.ml.registry import ModelRegistry
            registry = ModelRegistry(config=self.config)
            versions = registry.list_versions()
            if versions:
                regime_scorer = build_regime_aware_scorer(registry, versions, ctx.regime)
                if regime_scorer._models:
                    # ST-07: 统一评分入口 (与 get_scores 单股路径共用, 特征对齐一致)
                    preds = score_stocks(regime_scorer, [fd.factor_scores for fd in ctx.factor_daily])
                    for fd, p in zip(ctx.factor_daily, preds):
                        fd.ml_score = p
                    if not regime_scorer.feature_names:
                        logger.warning("ML scoring skipped: 生效模型无 feature_names")
                    elif versions:
                        # 记录生效模型版本 (与持久化口径一致: 取最新版本号)
                        ctx.ml_model_version = versions[-1].get("version", "")
        except Exception as e:
            logger.warning(f"ML scoring skipped: {e}")

        # Blend ML score into composite_score
        ml_weight = self.config.get("strategy.ml.mix_weight", 0.2) if self.config else 0.2
        for fd in ctx.factor_daily:
            if fd.ml_score != 0.0:
                fd.composite_score = fd.composite_score * (1 - ml_weight) + fd.ml_score * ml_weight

        # 持久化 ML 预测结果到 model_predictions 表
        try:
            if self.data_layer and hasattr(self.data_layer, "persist_model_predictions"):
                model_version = getattr(self, '_model_version', None)
                if not model_version:
                    try:
                        from strategy_layer.ml.registry import ModelRegistry
                        reg = ModelRegistry(config=self.config)
                        versions = reg.list_versions()
                        if versions:
                            model_version = versions[-1].get("version", "unknown")
                    except Exception:
                        model_version = "unknown"
                # ST-45: factor_score 与 blended_score 原来抄同一个 fd.composite_score,
                # 两列恒等 → blended_score 列无法回答"ML 到底改了多少分", factor_score 列名撒谎。
                # 现分别记录: blend 前 (重算) 的因子分与 blend 后的最终分。blend 公式与
                # 上方循环一致: composite*(1-w) + ml*w。
                _ml_w = ml_weight

                def _pre_blend(fd):
                    c = getattr(fd, 'composite_score', None)
                    if c is None:
                        return None
                    if fd.ml_score != 0.0:
                        return (c - _ml_w * fd.ml_score) / (1 - _ml_w)
                    return c

                rows = [
                    (fd.date, fd.code, fd.ml_score,
                     round(_pre_blend(fd), 6) or 0.0,
                     getattr(fd, 'composite_score', None) or 0.0,
                     model_version or "unknown")
                    for fd in ctx.factor_daily
                ]
                self.data_layer.persist_model_predictions(rows)
                logger.info(f"ML predictions persisted: {len(ctx.factor_daily)} rows for {ctx.date}")
        except Exception as e:
            logger.warning(f"model_predictions write skipped: {e}")

    def _stage_select(self, ctx: StrategyStageContext) -> bool:
        """Stage 4: L1 安全闸 + 选股。produces: selection。返回 True 表示 fail-closed 中止。"""
        # L1 安全闸: 批量取 ST/退市/停牌风险码
        # DL-04 fail-closed: get_risky_codes 返回 None (查询失败且无当日缓存) 时,
        # 中止本轮选股并告警, 不得拿空集放行 (原 fail-open 会让 ST/退市安全闸整个打开)。
        try:
            risky_codes = self.data_layer.get_risky_codes()
        except Exception as e:
            logger.error(f"get_risky_codes 异常 — L1 安全闸 fail-closed, 中止本轮选股: {e}")
            risky_codes = None
        if risky_codes is None:
            logger.error("L1 安全闸 fail-closed: 无法获取风险码, 本轮选股中止 (不产出候选)")
            return True
        ctx.selection = self.selector.run(ctx.factor_daily, ctx.regime, excluded_codes=risky_codes)
        return False

    def _buy_budget_codes(self, ctx: StrategyStageContext) -> set:
        """R-2 方案①: 推荐码预算集 — agent_gate 前预算 (推荐码在 signal_gen 才真出,
        阶段链 select→agent_gate→signal_gen 不许颠倒 P0-5 接线)。

        规则与 signal_generator.generate:62 逐位同构: 候选池内 code 不在
        current_positions → buy。持仓票出 hold 不算推荐 → 不进预算。
        同构由 test_budget_matches_signal_generator_buy_set 对拍钉守护 (防漂移假绿)。
        """
        positions = ctx.current_positions or {}
        return {
            c.code for c in ctx.selection.candidates
            if hasattr(c, "code") and c.code not in positions
        }

    def _stage_agent_gate(self, ctx: StrategyStageContext) -> None:
        """Stage 5: Agent 审核 (受配置开关控制)。produces: agent_reviews"""
        ctx.agent_gate_enabled = True
        if self.config:
            ctx.agent_gate_enabled = self.config.get("strategy.agent_gate.enabled", True) if self.config else True
        if ctx.agent_gate_enabled:
            # R-2 方案① (10-19): 推荐码预算 must_deep — rationale 只落 deep 腿,
            # 推荐码 (signal_gen buy) 若被前 N 截断在外 → 两表不相交 → 晚报占位。
            # 预算规则与 signal_gen 逐位同构 (候选池非持仓即 buy), 对拍钉守护。
            must_deep = self._buy_budget_codes(ctx)
            ctx.agent_reviews = self.deep_review.review_candidates(
                ctx.selection.candidates, ctx.date, must_deep=must_deep)
            # coverage log (判据5): 防「又静默分叉」#163 — 预算进没进 deep 集每日可见。
            # covered 用 review 结果反证: must_deep 码在 agent_reviews 里出现过 normal
            # 腿不丢 (deep 只影响辩论深度, 不影响是否出 review)。
            missing = sorted(c for c in must_deep
                             if c not in {r.code for r in ctx.agent_reviews})
            logger.info(f"deep coverage: must_deep={len(must_deep)} "
                        f"covered={len(must_deep) - len(missing)} missing={missing}")
            # P0-2 (2026-09-28): 加 verdict 闸 —— agent 不推荐的票同样剔除,
            # 不只过滤低信心票。判例对齐: enrich_reason 闸A (动作一致),
            # 「入池 buy 的票, agent verdict 必须与之相容」在策略层的落位。
            _REJECT_VERDICTS = {"不推荐", "观望", "警惕", "卖出", "规避"}
            # P0-2 止血 (2026-09-29): 去掉第三档「无判断=拒绝」。
            # 判例「用有病的产物当基线」: AgentReviewResult 永远没有 verdict 字段,
            # getattr 修复会把炸雷换成静默地雷（推荐池归零）。
            # verdict 闸完整版等 dataclass 补 verdict 字段 + deep_review 填后启用。
            # F-21 止血: 区分业务低置信与系统性降级兜底
            # 真实业务判断为低置信 (confidence<=0.3 且未触发降级兜底) -> 剔除
            # 系统性故障 (degraded_fallback=True, 如 LLM 410/429/超时返回 0.30) -> 保留放行 + warning 告警留痕, 绝不抹杀全集
            rejected_codes = set()
            degraded_codes = set()
            # F-21 钉3 (统筹 sm-c789b802b7b145e4): 明确看空闸 ——
            # verdict ∈ 明确看空词表 (不推荐/卖出/规避) 的票, 无论 confidence 高低、
            # 无论是否降级兜底, 一律剔除出候选池: 降级标识只决定「要不要告警」,
            # 不决定「要不要买」——保底放行 ≠ 推荐买入。
            # 刻意不含「观望」: 观望 = 「无明确方向」, 与 10-20 未调LLM兜底
            # (fallback_response 返 verdict=观望) 同形, 剔除它等于把 F-21
            # 止血打回原形 (15/15 全灭)。故看空词表用窄集, 观望交给置信闸。
            _REJECT_VERDICTS_HARD = {"不推荐", "卖出", "规避"}
            for r in ctx.agent_reviews:
                verdict = getattr(r, "verdict", "")
                if not isinstance(verdict, str):
                    verdict = ""
                verdict = verdict.strip()
                if verdict in _REJECT_VERDICTS_HARD:
                    rejected_codes.add(r.code)
                    continue
                if r.confidence <= 0.3:
                    if getattr(r, "degraded_fallback", False):
                        degraded_codes.add(r.code)
                    else:
                        rejected_codes.add(r.code)

            if degraded_codes:
                logger.warning(
                    f"Agent审核遇到系统性降级兜底: {len(degraded_codes)} 只候选股票未调LLM/超时 (confidence<=0.3), "
                    f"保留放行不静默抹杀候选: {sorted(degraded_codes)}"
                )

            if rejected_codes:
                before = len(ctx.selection.candidates)
                ctx.selection.candidates = [c for c in ctx.selection.candidates if c.code not in rejected_codes]
                logger.info(f"Agent审核过滤: {len(rejected_codes)}/{before} 只股票被剔除 (confidence<=0.3 业务判断)")
            logger.info(f"Agent审核: {len(ctx.agent_reviews)} 只股票已审核, "
                         f"{len(ctx.selection.candidates)} 只通过 (含降级保底 {len(degraded_codes)} 只)")
        else:
            logger.info("Agent gate disabled by config, skipping agent review")

    def _collect_entry_prices(self, ctx: StrategyStageContext) -> tuple[dict, dict]:
        """ST-16: 收集候选股入场基准价 + ATR, 保证 buy 推荐入场参数非空。

        价格: 优先当日 snapshot 收盘价; snapshot 缺 code (超时/未覆盖) 时
        用 get_history 最近一根 K 线收盘价兜底。
        ATR: 用 get_history 窗口内 mean(high-low) 近似真实波幅; 取不到则不填,
        由 generator 回退为价格 2% 固定百分比带。任何单股失败不阻塞整体。
        """
        stock_prices: dict[str, float] = {}
        try:
            snap = self.data_layer.snapshot(ctx.date) if self.data_layer else None
            if snap and hasattr(snap, 'klines'):
                stock_prices = {k.code: k.close for k in snap.klines
                                if hasattr(k, 'code') and hasattr(k, 'close')}
        except Exception as e:
            logger.warning(f"Signal price snapshot skipped: {e}")

        atr_values: dict[str, float] = {}
        if not self.data_layer or not ctx.selection:
            return stock_prices, atr_values
        for cand in ctx.selection.candidates:
            code = cand.code
            try:
                hist = self.data_layer.get_history(code, ctx.date, window=14)
                klines = getattr(hist, 'klines', None) or []
                if not klines:
                    continue
                # ATR 近似: 窗口内 mean(high-low)
                ranges = [k.high - k.low for k in klines if k.high > 0 and k.low > 0]
                if ranges:
                    atr_values[code] = round(sum(ranges) / len(ranges), 4)
                # 价格兜底: snapshot 缺该 code 时用最近收盘价
                if not stock_prices.get(code):
                    last_close = klines[-1].close
                    if last_close > 0:
                        stock_prices[code] = last_close
            except Exception as e:
                logger.debug(f"Entry price fallback skipped for {code}: {e}")
        return stock_prices, atr_values

    def _stage_signal_gen(self, ctx: StrategyStageContext) -> None:
        """Stage 6: 信号生成 + provenance 决策链溯源。produces: signals"""
        # ST-16: 入场参数口径 — entry 区间 = 近期收盘价 ± ATR*entry_mult 容差带,
        # stop_loss/take_profit 按 ATR 倍数带 (ATR 缺失时 generator 内回退价格 2% 固定百分比带)。
        # 价格获取: 优先当日 snapshot, 缺 code 时用 get_history 最近收盘价兜底,
        # 确保每个 buy 推荐 entry_low/entry_high/stop_loss/take_profit 非空
        # (否则执行层判"无入场参数"作废)。
        stock_prices, atr_values = self._collect_entry_prices(ctx)
        ctx.signals = self.signal_generator.generate(
            ctx.selection, current_positions=ctx.current_positions,
            stock_prices=stock_prices, atr_values=atr_values)

        # ST-13: provenance 决策链溯源 — 每条信号落一条可回溯的决策快照
        # (G45 首个接线的 G 件, 决策账本 S-09)。写入失败不阻塞信号产出。
        # 建表属写库操作: 默认关闭, 需用户 GO 后在 config 启用 strategy.provenance.enabled。
        prov_enabled = False
        if self.config:
            prov_enabled = bool(self.config.get("strategy.provenance.enabled", False))
        try:
            from strategy_layer.provenance import ProvenanceTracker, SignalProvenanceRecord
            if prov_enabled and self.db is not None:
                tracker = ProvenanceTracker(db=self.db)
                # 权重表版本指纹 (回答"用的哪版权重")
                weights_hash = ""
                if ctx.used_weights:
                    import hashlib as _hl
                    weights_hash = _hl.md5(
                        json.dumps({k: round(v, 6) for k, v in sorted(ctx.used_weights.items())},
                                   sort_keys=True).encode()).hexdigest()[:8]
                # Agent 审核结论按 code 索引
                agent_by_code = {}
                if ctx.agent_gate_enabled:
                    for r in ctx.agent_reviews:
                        agent_by_code[r.code] = r
                # 因子分按 code 索引 (算 top-N 贡献)
                fd_by_code = {fd.code: fd for fd in ctx.factor_daily}
                for sig in ctx.signals:
                    # P0-5 (2026-09-28): 五条线连通 — agent verdict 写进 reason。
                    # 现状: stock_pool.added_reason = 「候选池第X位」排名话术,
                    # agent verdict 从没进 stock_pool (策略排名与 agent 辩论是两条平行线)。
                    # 修法: P0-2 过滤拒绝票后, 留下的票的 reason 带 agent verdict。
                    # 格式: 「agent 推荐（verdict=X, confidence=Y）| 策略排名（候选池第N位）」
                    # —— 用户先看 agent 结论, 再看策略排名 (可回溯)。agent_gate disabled 时零改动。
                    # P0-5 写入侧第十变体修 (2026-10-04): 抽出 _pick_reason_part 工具,
                    # 闸拒 set 守 + 空 verdict log 显形 (第十变体判死区)。
                    agent = agent_by_code.get(sig.code)
                    _pick_reason_part(agent, sig)
                    fd = fd_by_code.get(sig.code)
                    contribs: dict[str, float] = {}
                    if fd and ctx.used_weights:
                        raw = {f: (ctx.used_weights.get(f, 0.0) * v)
                               for f, v in fd.factor_scores.items() if ctx.used_weights.get(f, 0.0)}
                        contribs = dict(sorted(raw.items(), key=lambda kv: abs(kv[1]),
                                               reverse=True)[:5])
                    agent = agent_by_code.get(sig.code)
                    tracker.record_signal_provenance(SignalProvenanceRecord(
                        code=sig.code, date=sig.date, action=sig.action,
                        signal_id=sig.signal_id,
                        composite_score=round(fd.composite_score, 4) if fd else 0.0,
                        ml_score=round(fd.ml_score, 4) if fd else 0.0,
                        regime=ctx.regime, model_version=ctx.ml_model_version,
                        weights_hash=weights_hash, factor_contributions=contribs,
                        agent_confidence=getattr(agent, "confidence", None),
                        agent_reason=getattr(agent, "reason", "") or "",
                        run_id=ctx.run_id,  # #79 父子关联: signal 行回填 run 标识
                    ))
                logger.info(f"Provenance: {len(ctx.signals)} signal decision records written")
        except Exception as e:
            logger.warning(f"Provenance write skipped: {e}")

        # ST-08 拆假货: 原此处硬编码 pre_market=None (假契约, 盘前简报在主管道中永不产出)。
        # 盘中 Phase 1 真重建排队 (前置 DL-22 竞价采集 + 真实分钟数据, 决策账本 S-05),
        # 届时以独立 API 产出, 不在 run_daily 内回填假字段。

    @catch_quantstar_error(layer_name="strategy")
    @with_timeout(timeout_s=30)
    def get_scores(self, code: str, date: str) -> FactorScores:
        from strategy_layer._models import FactorScores
        from strategy_layer.aggregation.aggregator import Aggregator
        matrix = self.factor_engine.compute_all([code], date)
        scores = matrix[code] if code in matrix else {}

        ml_score = 0.0
        try:
            # ST-07: 两条 ML 评分路径统一 — 与 run_daily 批量路径同走 RegimeAwareScorer +
            # score_stocks (按 feature_names 对齐 + 过滤 quarantined 模型), 替代原
            # MetaScorer + sorted(keys) 字母序对齐 (特征错位 → 同股不同分, 审计 S-08)。
            from strategy_layer.ml.regime_scorer import build_regime_aware_scorer, score_stocks
            from strategy_layer.ml.registry import ModelRegistry
            registry = ModelRegistry(config=self.config)
            versions = registry.list_versions()
            if versions and scores:
                regime = self.market_regime(date)
                scorer = build_regime_aware_scorer(registry, versions, regime)
                ml_score = score_stocks(scorer, [scores])[0]
        except Exception:
            pass

        composite = Aggregator.aggregate_single(scores, ml_score=ml_score)
        return FactorScores(code=code, date=date, factors=scores, composite_score=composite)

    @catch_quantstar_error(layer_name="strategy")
    @with_timeout(timeout_s=30)
    def get_signals(self, code: str, date: str) -> TradeSignals:
        from strategy_layer._models import TradeSignals
        result = self.get_scores(code, date)
        composite = result.composite_score
        signal = "buy" if composite > 0.5 else ("sell" if composite < -0.5 else "hold")
        return TradeSignals(code=code, date=date, signal=signal, strength=abs(composite), composite_score=composite)

    @catch_quantstar_error(layer_name="strategy")
    @with_timeout(timeout_s=15)
    def market_regime(self, date: str) -> str:
        """多指标综合判断市场状态: 趋势 + 宽度 + 量能

        ST-12 标注 (2026-08-18, 审计 S-16): "量能"维度是假维度 — volume_score 由
        涨跌幅变换而来 ((avg_change+3)/6), 未使用任何成交量/成交额数据。
        regime 判定实际只有趋势+宽度两维。接真实量能 (全市场成交额 vs 20 日均值)
        为排队项; 在接入前本注释为唯一真相源, 不得再宣称三维判定。
        """
        if not self.data_layer:
            return "neutral"
        try:
            snapshot = self.data_layer.snapshot(date)
            klines = snapshot.klines if snapshot else []
            if not klines:
                return "neutral"

            # 1. 趋势 (当日涨跌幅均值)
            changes = [k.change_pct for k in klines if hasattr(k, "change_pct")]
            if not changes:
                return "neutral"
            avg_change = sum(changes) / len(changes)

            # 2. 宽度 (上涨家数占比)
            advancing = sum(1 for c in changes if c > 0)
            breadth = advancing / len(changes) if changes else 0.5

            # 3. 量能 — ST-12 标注: 假维度, 未使用任何成交量数据 (由涨跌幅变换而来, S-16)。
            # 接真实量能 (全市场成交额 vs 20 日均值) 为排队项, 见方法 docstring。
            volume_score = min(1.0, max(0.0, (avg_change + 3) / 6))  # [-3,3] → [0,1]

            # 4. 综合评分
            score = avg_change * 0.4 + (breadth - 0.5) * 10 * 0.3 + volume_score * 0.3

            self._last_regime_metrics = {
                "avg_change": round(avg_change, 4),
                "breadth": round(breadth, 4),
                "volume_score": round(volume_score, 4),
                "composite_score": round(score, 4),
            }

            if score > 1.5:
                return "bull"
            elif score < -1.5:
                return "bear"
            elif abs(avg_change) < 0.5 and 0.35 < breadth < 0.65:
                return "oscillating"
            else:
                return "neutral"
        except Exception:
            self._last_regime_metrics = {}
            return "neutral"

    def run_pre_market(self, date: str) -> PreMarketBrief:
        """Phase 1: 盘前简报 — 集合竞价分析

        ST-08 拆假货 (2026-08-18): 原实现读 IntradaySnapshot 不存在的 change_pct/
        volume_ratio 属性, getattr 默认值恒成立 → 每只股票恒返回假数据 (审计 S-05)。
        假实现已删除 — 宁可没有, 不可是假的 (总原则 5)。
        真重建排队: 前置 DL-22 竞价采集能力 + 真实分钟数据 (决策账本 S-05)。
        """
        raise NotImplementedError(
            "run_pre_market 假实现已拆除 (ST-08): 原实现恒返回默认值假数据。"
            "真重建排队中, 前置: DL-22 竞价采集 + 真实分钟数据")

    @catch_quantstar_error(layer_name="strategy")
    @with_timeout(timeout_s=30)
    def run_intraday_cycle(self, timestamp: str) -> list[IntradayAlert]:
        """Phase 2: 盘中监控 — 分钟级技术指标 + 异常检测"""
        logger.info(f"Intraday Phase 2 — cycle at {timestamp}")
        codes = self._state.get("intraday_codes", []) or self._get_stock_codes(timestamp[:10])
        alerts = []
        # 分钟级技术分析
        for code in codes[:20]:
            try:
                bars = self.data_layer.snapshot_intraday(timestamp) if self.data_layer else None
                if bars and hasattr(bars, "minute_data") and bars.minute_data:
                    signal = self.intraday_monitor.analyze_minute(code, bars.minute_data)
                    if signal:
                        alerts.append(IntradayAlert(
                            code=code,
                            name=code,
                            trigger_reason=signal["type"],
                            current_price=signal["analysis"].get("price", 0),
                            trigger_time=timestamp,
                            urgency="中",
                        ))
            except Exception as e:
                logger.warning(f"Minute analysis failed for {code}: {e}")
        # 传统异常检测 (涨跌幅)
        scan_signals = self.intraday_monitor.scan(codes, timestamp[:10])
        for s in scan_signals:
            alerts.append(IntradayAlert(
                code=s.get("code", ""),
                name=s.get("code", ""),
                trigger_reason=s.get("type", "unknown"),
                current_price=0.0,
                trigger_time=timestamp,
                urgency="中",
            ))
        return alerts

    @catch_quantstar_error(layer_name="strategy")
    @with_timeout(timeout_s=30)
    def run_post_market(self, date: str, returns: list[float] | None = None,
                         dates: list[str] | None = None) -> PostMarketResult:
        """Phase 3: 盘后总结"""
        logger.info(f"Intraday Phase 3 — post-market for {date}")
        dashboard_data = None
        try:
            from strategy_layer.reporting.dashboard import PerformanceDashboardBuilder
            builder = PerformanceDashboardBuilder()
            if returns and dates:
                dashboard = builder.build(date, returns)
                dashboard_data = {
                    "metrics": {
                        "sharpe": dashboard.metrics.sharpe,
                        "annualized_return": dashboard.metrics.annualized_return,
                        "max_drawdown": dashboard.metrics.max_drawdown,
                        "win_rate": dashboard.metrics.win_rate,
                    },
                    "monthly_returns": builder.monthly_returns(returns, dates),
                }
        except Exception as e:
            logger.debug(f"Dashboard skip: {e}")
        return PostMarketResult(
            date=date, status="completed",
            alerts_count=len(self._state.get("intraday_alerts", [])),
            dashboard=dashboard_data,
        )

    def _get_stock_codes(self, date: str) -> list[str]:
        """获取当日股票列表"""
        if not self.data_layer:
            return []
        try:
            snapshot = self.data_layer.snapshot(date)
            if snapshot and snapshot.klines:
                return [k.code for k in snapshot.klines if hasattr(k, "code")]
        except Exception as e:
            logger.warning(f"Failed to get stock codes: {e}")
        return []

    def get_daily_regime(self, date_range: tuple[str, str] | str) -> list[dict]:
        """ST-15: 读 API — 查询指定日期或日期范围的市场状态记录 (BT-08 前置)

        Args:
            date_range: 单日字符串 "2026-08-19" 或日期区间元组 ("2026-08-01", "2026-08-19")

        Returns:
            list[dict]: daily_regime 行记录列表，按日期升序排列
        """
        if not self.db:
            return []
        try:
            self._ensure_tables()
            conn = self.db.get_connection()
            if isinstance(date_range, (tuple, list)):
                start_d, end_d = date_range[0], date_range[1]
                rows = conn.execute(
                    "SELECT * FROM daily_regime WHERE date BETWEEN ? AND ? ORDER BY date ASC",
                    (start_d, end_d)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM daily_regime WHERE date = ?", (str(date_range),)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"get_daily_regime query failed: {e}")
            return []

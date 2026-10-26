"""backtest_layer/evaluation/agent_eval.py — Agent 预测评估 (BT-09)

回测层是全系统的评估供给中心: 读 agent_predictions + factor_daily.fwd_return_5d
(真实前向收益), 产出命中率 / 分 verdict / 分 agent / 分置信度 tier 统计,
并产出 forward_returns dict 供 ops _run_agent_evaluate 喂给 Agent 层 Evaluator
(AG-01 闭环: 回测层生产, ops 消费, Evaluator 回写 actual_return/hit)。

单位约定: factor_daily.fwd_return_5d 库内为百分数 (1.92 = +1.92%),
Evaluator 的命中判定用小数 (abs(actual) < 0.02), 本模块产出统一转小数。

TODO (DL-24): 现阶段直读 factor_daily.fwd_return_5d 现有列;
DL-24 跨层评估数据源统一读 API 就绪后切换为该 API (一处供给, 多环消费)。
"""

from dataclasses import dataclass, field

from common.logging import get_logger

logger = get_logger("backtest.agent_eval")

# 置信度分层阈值 (high/mid/low)
TIER_THRESHOLDS = (0.6, 0.4)


def is_hit(verdict: str, actual_return: float) -> bool:
    """命中判定 — 与 Agent 层 Evaluator 单口径对齐 (卡IV, 2026-08-30):

    推荐 且实际上涨 = 命中; 不推荐 且实际下跌 = 命中;
    观望/警惕 (中性带, 无方向主张) 且 |收益| < 2% = 命中;
    其余 = 未命中。

    口径裁定 (以信号语义/PIT 为准): 警惕=风险提示或依据缺失,
    参考信息不构成方向阻断 (output_spec AG-03) — 无方向主张不能
    用方向兑现判错, 与观望同构入 2% 小波动容差带。旧实现把警惕
    落穿算 miss 系抄 AG 语义时漏枚举值 (docstring 自称对齐但
    实现分叉), 同一 verdict 两套口径污染 agent_eval 命中率统计。

    入参先 normalize_verdict (AG 同款): legacy 词 (approve/reject/
    hold/watch/关注…) 归一化后再判 — 归一化前两处对 legacy 词
    行为不一致 (AG 归一化走宽判 / BT 落穿算 miss), 隐藏分叉第二层。
    """
    from agent_layer.core.output_spec import normalize_verdict
    verdict = normalize_verdict(verdict)
    if verdict == "推荐" and actual_return > 0:
        return True
    if verdict == "不推荐" and actual_return < 0:
        return True
    if verdict in ("观望", "警惕") and abs(actual_return) < 0.02:
        return True
    return False


def confidence_tier(confidence: float | None) -> str:
    """置信度分层: high ≥ 0.6, mid 0.4~0.6, low < 0.4, unknown 缺失"""
    if confidence is None:
        return "unknown"
    if confidence >= TIER_THRESHOLDS[0]:
        return "high"
    if confidence >= TIER_THRESHOLDS[1]:
        return "mid"
    return "low"


@dataclass
class AgentEvalResult:
    date: str
    total_predictions: int = 0
    evaluated: int = 0              # 能配到真实前向收益的预测数
    unevaluated: int = 0            # 前向收益缺失 (未到期/未回填), 不造假值
    hits: int = 0
    hit_rate: float | None = None   # None = 不可评估 (evaluated=0)
    by_verdict: dict = field(default_factory=dict)
    by_agent: dict = field(default_factory=dict)
    by_tier: dict = field(default_factory=dict)
    # {code: 小数收益} — 供 ops 喂给 Agent 层 Evaluator (AG-01)
    forward_returns: dict = field(default_factory=dict)


class AgentEvaluator:
    """Agent 预测评估器 — 读 agent_predictions + factor_daily.fwd_return_5d"""

    def __init__(self, db):
        self.db = db

    def get_forward_returns(self, date: str) -> dict[str, float]:
        """取指定日期的真实 5 日前向收益, 转为小数单位。

        TODO (DL-24): 切换为跨层评估数据源统一读 API。
        """
        if not self.db:
            return {}
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT code, fwd_return_5d FROM factor_daily "
            "WHERE date = ? AND fwd_return_5d IS NOT NULL",
            (date,),
        ).fetchall()
        return {r["code"]: r["fwd_return_5d"] / 100.0 for r in rows}

    def find_evaluable_date(self, max_lookback: int = 30) -> str | None:
        """找最近一个"可评估日": 该日有未结算预测 (hit IS NULL) 且
        factor_daily 已有该日的 fwd_return_5d 数据 (前向窗口已到期)。

        预测后 5 个交易日窗口未到期时 fwd_return_5d 为 NULL, 该日不可评估 —
        返回 None 而不是拿空 dict 冒充评估完成 (宁可没有, 不可是假的)。
        """
        if not self.db:
            return None
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT DISTINCT p.date FROM agent_predictions p "
            "JOIN factor_daily f ON f.date = p.date AND f.fwd_return_5d IS NOT NULL "
            "WHERE p.hit IS NULL "
            "ORDER BY p.date DESC LIMIT ?",
            (max_lookback,),
        ).fetchall()
        return rows[0]["date"] if rows else None

    def evaluate(self, date: str) -> AgentEvalResult:
        """评估指定日期的全部 Agent 预测, 产出分组统计。

        配不到前向收益的预测计入 unevaluated, 不冒充命中/未命中 —
        宁可没有, 不可是假的。
        """
        result = AgentEvalResult(date=date)
        if not self.db:
            return result
        conn = self.db.get_connection()
        preds = conn.execute(
            "SELECT agent_name, code, verdict, confidence "
            "FROM agent_predictions WHERE date = ?",
            (date,),
        ).fetchall()
        result.total_predictions = len(preds)
        if not preds:
            return result

        fwd = self.get_forward_returns(date)
        result.forward_returns = fwd

        for row in preds:
            code = row["code"]
            verdict = row["verdict"]
            agent = row["agent_name"] or "unknown"
            tier = confidence_tier(row["confidence"])

            v_bucket = result.by_verdict.setdefault(
                verdict, {"total": 0, "evaluated": 0, "hits": 0, "hit_rate": None})
            a_bucket = result.by_agent.setdefault(
                agent, {"total": 0, "evaluated": 0, "hits": 0, "hit_rate": None})
            t_bucket = result.by_tier.setdefault(
                tier, {"total": 0, "evaluated": 0, "hits": 0, "hit_rate": None})
            for bucket in (v_bucket, a_bucket, t_bucket):
                bucket["total"] += 1

            if code not in fwd:
                result.unevaluated += 1
                continue

            actual = fwd[code]
            hit = is_hit(verdict, actual)
            result.evaluated += 1
            if hit:
                result.hits += 1
            for bucket in (v_bucket, a_bucket, t_bucket):
                bucket["evaluated"] += 1
                if hit:
                    bucket["hits"] += 1

        def _finalize(buckets: dict) -> None:
            for bucket in buckets.values():
                if bucket["evaluated"] > 0:
                    bucket["hit_rate"] = round(bucket["hits"] / bucket["evaluated"], 4)

        _finalize(result.by_verdict)
        _finalize(result.by_agent)
        _finalize(result.by_tier)
        if result.evaluated > 0:
            result.hit_rate = round(result.hits / result.evaluated, 4)

        logger.info(
            f"Agent eval {date}: {result.evaluated}/{result.total_predictions} evaluated, "
            f"hit_rate={result.hit_rate}")
        return result

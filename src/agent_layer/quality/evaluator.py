"""盘后评估

AG-01 评估口径 (统一):
- forward_returns 为小数口径 (0.05 = +5%); DL-24 get_fwd_returns 返回百分点,
  evaluate_predictions() 换算成小数后调用 evaluate()
- hit 判定: 推荐+actual>0 / 不推荐+actual<0 → hit=1;
  观望/警惕 + |actual|<2% → hit=1 (判对无明确方向); 其余 hit=0
- 收益缺失 (code 不在截面 / None = 未兑现) → 跳过 (skipped 计数), hit 保持 NULL
  等后续交易日回填, 绝不记造假 0 收益评估
"""

from agent_layer.core.output_spec import normalize_verdict
from agent_layer.quality.repository import AgentQualityRepository
from common.logging import get_logger

logger = get_logger("agent.quality.evaluator")


def is_hit_ag01(verdict: str, actual: float) -> bool:
    """AG-01 hit 判定 canonical 唯一实现 (#112 收敛: 结算核心与 backfill 脚本均 import 此处).

    推荐+actual>0 / 不推荐+actual<0 → 命中;
    观望·警惕 (无方向主张) + |actual|<2% → 命中; 其余 miss。
    actual 为小数 (百分点须先 ÷100)。verdict 须先经 normalize_verdict 归一。
    """
    if verdict == "推荐":
        return actual > 0
    if verdict == "不推荐":
        return actual < 0
    return abs(actual) < 0.02  # 观望 / 警惕


class Evaluator:
    def __init__(self, database=None):
        self.repo = AgentQualityRepository(database)
        self.repo.ensure_tables()

    def evaluate(self, date: str, forward_returns: dict[str, float]) -> dict:
        rows = self.repo.get_unresolved_predictions(date)
        hits = 0
        misses = 0
        skipped = 0
        for row in rows:
            actual = forward_returns.get(row["code"])
            if actual is None:
                # AG-01: 未兑现收益 → 跳过不造假 (原 .get(code, 0) 把未兑现造假为 0 收益评估)
                skipped += 1
                continue
            # AG-03: 存量历史行读时归一化到统一词表
            verdict = normalize_verdict(row["verdict"])
            is_hit = is_hit_ag01(verdict, actual)
            self.repo.update_prediction_result(row["id"], actual, 1 if is_hit else 0)
            if is_hit:
                hits += 1
            else:
                misses += 1
        total = hits + misses
        if total:
            logger.info(f"Evaluator: {date} → {hits}/{total} hits ({hits/total*100:.1f}%), {skipped} skipped")
        else:
            logger.info(f"Evaluator: {date} → no predictions ({skipped} skipped)")
        return {"evaluated": total, "hits": hits, "misses": misses, "skipped": skipped}

    def evaluate_predictions(self, date: str, data_layer=None, horizon: int = 5) -> dict:
        """AG-01 评估核心: 读 agent_predictions + DL-24 get_fwd_returns 截面, 回填 hit/actual_return。

        - get_fwd_returns 返回百分点 {code: 5.0}, 只含已兑现 (非 NULL) 值; 此处换算成小数后传给 evaluate()
        - 缺席 code / 无读 API / 异常 → 诚实降级全跳过, 绝不造假
        """
        cross_section: dict[str, float] = {}
        if data_layer is not None and hasattr(data_layer, "get_fwd_returns"):
            try:
                raw = data_layer.get_fwd_returns(date, horizon) or {}
                cross_section = {code: ret / 100.0 for code, ret in raw.items() if ret is not None}
            except Exception as e:
                logger.warning(f"Evaluator: get_fwd_returns failed for {date}: {e} (诚实降级, 全跳过)")
        return self.evaluate(date, cross_section)

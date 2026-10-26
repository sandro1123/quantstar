"""RiskProfileService — 个股风险档案 L1 防雷 (卡#20 第一段, 统筹 sm-3f5181e5931d4ae8)。

统筹裁定: mine_clearance_tdx = 风险档案 (score/f_type/s_type/reason), 非解禁日历。
选股日批量拉候选股档案, score >= RISK_SCORE_GATE 的码集供 L1 过滤 —
与 get_risky_codes (ST/退市/停牌, DL-04) 并列的第二风险面。

fail-closed 语义 (DL-04 同族): 源不可用 → None (调用方必须不放行), 不是空集。
逐股一调形态: 调用次数 = 候选数 (拉取量预算 = 候选数 × 1 调, 见测试锁)。
"""
from common.logging import get_logger

logger = get_logger("data.risk_profile")

# 风险分闸 — 统筹裁定施工时按分布定, 默认 77 (09-12 首真拉分布账: score 中位 77, 用户 GO 60→77 校准)。
# adata score 语义: 风险评分 (观测样本 000002=64 财务类风险族 / 600519 部分行 60+)。
RISK_SCORE_GATE = 77


class RiskProfileService:
    """个股风险档案批量拉取 + 高风险码集。"""

    def __init__(self, source_registry=None, score_gate: float | None = None):
        self.source_registry = source_registry
        self.score_gate = float(score_gate if score_gate is not None else RISK_SCORE_GATE)
        self._source = None

    def _get_source(self):
        if self._source is not None:
            return self._source
        if self.source_registry is None:
            return None
        self._source = self.source_registry.get_best_source("mine_clearance")
        return self._source

    def fetch_risky_codes(self, candidate_codes: list[str]) -> set[str] | None:
        """批量拉候选股风险档案, 返回 score >= gate 的码集。

        Returns:
            set[str]: 高风险码集 (可为空集 = 全过)
            None: 源不可用 (fail-closed, 调用方必须中止选股并告警)
        """
        if not candidate_codes:
            return set()
        source = self._get_source()
        if source is None:
            logger.warning("risk_profile: 无可用 mine_clearance 源 — fail-closed 返回 None")
            return None
        risky: set[str] = set()
        try:
            for code in candidate_codes:
                rows = source.get_data("mine_clearance", {"code": code}) or []
                for r in rows:
                    score = r.get("score")
                    if score is not None and float(score) >= self.score_gate:
                        risky.add(r.get("code", code))
                        break  # 单股一行达标即入集
        except Exception as e:
            logger.error(f"risk_profile: 拉取失败 fail-closed: {e}")
            return None
        if risky:
            logger.info(f"risk_profile: {len(risky)}/{len(candidate_codes)} 只 "
                        f"score>={self.score_gate} 入 L1 防雷")
        return risky

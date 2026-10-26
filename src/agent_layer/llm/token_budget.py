"""Token Budget 限制 — 按 task 分配 token 预算，超限自动降级

2026-09-24 辩论腿止血（统筹批·不涉成本部分）：
  病案：辩论六专家腿连续三天全断，日志实文 `[expert:tech] token budget exceeded`。
  根因两条（同族：信号常量化 + 防线留了没人接线）：
    ① 预算耗尽后**永不恢复** —— reset() 全树零调用点、无持久化、无按日切分；
       而 TokenBudget 是 ProviderManager 的进程内实例（gunicorn 实测连续 4 天+），
       一旦耗尽该进程余生辩论腿全灭。
    ② 记账口径 `len(str(result)) // 3` 把字符数当 token，与真实消耗不同源。

  修法（本卡只修机制，不改额度数值——额度重设待用户裁成本）：
    ① 惰性按日自愈：每次读写前比对日期，跨日自动清零。
       **不依赖任何调用点记得调 reset**（否则管道改造后又会重演「防线没人接线」）。
    ② 记账优先真实 usage（provider 回传），估算为兜底且**口径可辨**（usage_source），
       审计要能看出一个数是哪来的。
"""
from common.logging import get_logger

logger = get_logger("agent.token_budget")

TASK_BUDGETS = {
    # ===== 额度全表 (统筹算账 + 用户 2026-09-25 裁定) =====
    # 定位: budget 的真作用是**防失控**(异常循环烧穿), 不是省钱 —— 原值按
    # 「省钱」设计, 结果正常用量就被卡死 (09-24 实测 expert_debate 6000 在
    # 16:39 耗尽, 当日辩论腿全断 132 次 [expert:*] token budget exceeded)。
    # 新原则 = 正常用量 × 10 倍余量。
    #
    # 成本实测账 (辩论腿走 cbcn/glm-5.2, 倍率 0.47 且 prompt token 不计费只算补全):
    #   全覆盖 deep_top_n 10只 × 6专家 = 60次/天 × ~500 补全token ≈ 3万补全token
    #   ≈ 14 积分/天; 用户池存量 9365 + 每日签到回血 300 → 回血是消耗的 21 倍。
    #   用户亲定 (09-03 在册, 09-25 重申): 「积分不是约束, 质量优先」。
    #
    # 改这些值须重走用户批准 (注释即批准锚, 勿凭感觉调)。
    "morning_report": 50000,
    "evening_report": 50000,
    "urgent_analysis": 20000,
    "stock_analysis": 10000,
    "entry_check": 5000,
    "exit_review": 5000,
    "quick_check": 2000,
    "debate": 50000,
    "committee": 50000,
    # 六专家 LLM 腿: 10只×6专家×~500补全token×10倍余量 = 30万
    "expert_debate": 300000,
    "multi_agent_review": 100000,
    "default": 5000,
}

# 记账口径标记（审计可辨：这个数是真实回传的还是估算的）
SOURCE_REAL = "real"
SOURCE_ESTIMATED = "estimated"


def _default_today() -> str:
    """生产默认时钟（北京日期）。测试经 today_fn 注入确定性时钟。"""
    try:
        from common.utils import bj_date
        return bj_date()
    except Exception:
        return ""


class TokenBudget:
    def __init__(self, today_fn=None):
        self._usage: dict[str, int] = {}
        self._source: dict[str, str] = {}
        self._today_fn = today_fn or _default_today
        self._day: str = ""

    # ---------- 按日自愈 ----------

    def _roll_if_new_day(self) -> None:
        """惰性跨日清零：读取当前日期，与上次记账日不同则整表清零。
        这是「防线自带载体」——不依赖任何调用点主动调 reset。"""
        try:
            today = self._today_fn()
        except Exception:
            return
        if not today:
            return
        if self._day and today != self._day:
            logger.info(f"token budget 跨日重置: {self._day} → {today}")
            self._usage.clear()
            self._source.clear()
        self._day = today

    @staticmethod
    def _bucket(task: str) -> str:
        # AG-COMPOSE: call_point 前缀归桶 — expert:sentiment 等 6 专家
        # 共享 expert_debate 池 (30万/日, 09-25 重设), 避免各落 default 秒空。
        if task.startswith("expert:"):
            return "expert_debate"
        if task.startswith("multi_agent_review:"):
            return "multi_agent_review"
        return task

    def get_budget(self, task: str) -> int:
        return TASK_BUDGETS.get(self._bucket(task), TASK_BUDGETS["default"])

    # ---------- 记账 ----------

    def record_usage(self, task: str, tokens: int = 0, text: str = "",
                     real_tokens: int | None = None):
        """记一次用量。

        优先级：real_tokens（provider 真实 usage）> text 估算 > tokens 显式值。
        口径写进 _source，供 usage_source() 审计。
        """
        self._roll_if_new_day()
        b = self._bucket(task)
        if real_tokens is not None:
            n, src = max(0, int(real_tokens)), SOURCE_REAL
        elif text:
            n, src = max(0, len(str(text)) // 3), SOURCE_ESTIMATED
        elif tokens:
            n, src = max(0, int(tokens)), SOURCE_ESTIMATED
        else:
            n, src = 0, SOURCE_ESTIMATED
        self._usage[b] = self._usage.get(b, 0) + n
        # 口径标记：真实优先（后到的估算不覆盖既有的真实）
        if src == SOURCE_REAL or b not in self._source:
            self._source[b] = src

    def remaining(self, task: str) -> int:
        self._roll_if_new_day()
        b = self._bucket(task)
        used = self._usage.get(b, 0)
        budget = self.get_budget(b)
        return max(0, budget - used)

    def is_exceeded(self, task: str) -> bool:
        return self.remaining(task) <= 0

    def usage_source(self, task: str) -> str | None:
        """该桶当前的记账口径（real/estimated）；未使用过返回 None。"""
        self._roll_if_new_day()
        return self._source.get(self._bucket(task))

    # ---------- 显式重置（手动干预口） ----------

    def reset(self):
        """重置所有任务用量"""
        self._usage.clear()
        self._source.clear()

    def reset_task(self, task: str):
        """重置指定任务的用量"""
        b = self._bucket(task)
        self._usage.pop(b, None)
        self._source.pop(b, None)

    def get_all_usage(self) -> dict:
        self._roll_if_new_day()
        return {k: {"budget": self.get_budget(k), "used": v, "remaining": self.remaining(k)}
                for k, v in self._usage.items()}

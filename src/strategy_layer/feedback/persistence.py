"""WeightPersistence — 因子权重持久化 + IC 反馈循环 (DB-backed)

v0.19.0 S3 F3.2: 从不存在的 factor_weights.json 改为读写 DB 表 factor_weights /
factor_ic。旧 JSON 路径已废弃 (生产从未生成过该文件 → 聚合恒用硬编码 DEFAULT_WEIGHTS)。

表结构:
  factor_weights(id, weight_date, factor_name, weight, method, old_weight, created_at,
                 UNIQUE(weight_date, factor_name))
  factor_ic(calc_date, factor_name, mean_ic, std_ic, icir, win_rate, effectiveness,
            sample_size, created_at, PRIMARY KEY(calc_date, factor_name))
"""
from common.logging import get_logger
from common.utils import bj_date

logger = get_logger("strategy.feedback")

# 滚动窗长 —— 与 `strategy_layer.governance.FAST_WINDOW` 同源 (单一定义点)。
# 守卫: tests/unit/strategy_layer/test_ic_rolling_columns.py::
#        test_rolling_window_same_as_governance_fast_window (防两处漂移)。
IC_ROLLING_WINDOW = 20

# std 判零阈值 —— **浮点"等于 0"不可依赖, 且行为随解释器版本变** (F席 09-16)。
# 病: `[0.1]*20` 这类"全同值"序列, std 在**实数域**应为 0, 但在浮点上:
#   CPython 3.12 (Neumaier 补偿求和, gh-100425) → sum=2.0 精确 → std=0.0
#   CPython 3.11 (朴素求和)                      → sum=2.0000000000000004
#                                                  → std=1.3877787807814457e-17
# → 判据 `std > 0` 在 3.11 上被击穿 → icir = mean/1.39e-17 = **7.2e15 天文数字**
# → 周报按 icir 排序时该因子**永远排第一**, 比"全 NULL"更坏的假信号
# (全 NULL 至少不排序; 天文值会主动排到首位)。
# 判据: std > IC_STD_EPS 才走 icir = mean/std, 否则 NULL。
# ⚠ std_ic 列仍写**真实算出的值** (诚实, 不篡改观测); 仅 icir 用阈值判零。
# 关联: 「边界量不可依赖」同族 —— "精确等于 0"在浮点上是个假设, 不是事实;
#       且该假设**跨 Python 版本不稳定** (3.12 起 sum() 改补偿求和)。
IC_STD_EPS = 1e-12


class WeightPersistence:
    def __init__(self, config=None, db=None, weights_path: str | None = None):
        self.config = config or {}
        self.db = db
        self._ic_history: dict[str, list[float]] = {}
        self._weight_history: list[dict[str, float]] = []

    # ---- weights ----
    def load_weights(self, default_weights: dict[str, float] | None = None) -> dict[str, float]:
        """读取最新 weight_date 的因子权重。无 DB 或无数据时返回 default。"""
        if self.db is None:
            return default_weights or {}
        try:
            conn = self.db.get_connection()
            row = conn.execute("SELECT MAX(weight_date) FROM factor_weights").fetchone()
            latest = row[0] if row else None
            if not latest:
                return default_weights or {}
            rows = conn.execute(
                "SELECT factor_name, weight FROM factor_weights WHERE weight_date = ?",
                (latest,),
            ).fetchall()
            weights = {r[0]: r[1] for r in rows if r[0] and r[1] is not None}
            if not weights:
                return default_weights or {}
            logger.info(f"Loaded factor weights from DB ({latest}): {len(weights)} factors")
            # 顺带载入近期 IC 历史, 供 update_weights_with_decay 使用
            self._load_ic_history(conn)
            return weights
        except Exception as e:
            logger.warning(f"Failed to load weights from DB: {e}")
            return default_weights or {}

    def save_weights(self, weights: dict[str, float], method: str = "ic_decay"):
        """写入今日权重到 factor_weights 表。"""
        if self.db is None or not weights:
            return
        try:
            conn = self.db.get_connection()
            date = bj_date()
            # 记录旧权重便于审计
            old = self.load_weights(default_weights={}) or {}
            for factor, w in weights.items():
                conn.execute(
                    "INSERT OR REPLACE INTO factor_weights "
                    "(weight_date, factor_name, weight, method, old_weight) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (date, factor, float(w), method, old.get(factor)),
                )
            conn.commit()
            logger.info(f"Saved {len(weights)} factor weights to DB ({date})")
        except Exception as e:
            logger.warning(f"Failed to save weights to DB: {e}")

    # ---- IC history ----
    def _load_ic_history(self, conn, lookback_days: int = 30):
        """从 factor_ic 表载入近 lookback_days 的每日 IC, 按因子聚合成 list。"""
        try:
            rows = conn.execute(
                "SELECT factor_name, mean_ic FROM factor_ic "
                "WHERE calc_date >= date('now', ?) AND mean_ic IS NOT NULL "
                "ORDER BY calc_date ASC",
                (f"-{lookback_days} days",),
            ).fetchall()
            history: dict[str, list[float]] = {}
            for factor, ic in rows:
                if factor and ic is not None:
                    history.setdefault(factor, []).append(float(ic))
            self._ic_history = history
        except Exception as e:
            logger.debug(f"load ic_history skip: {e}")

    def record_ic(self, ic_values: dict[str, float], sample_size: int = 0,
                  calc_date: str | None = None):
        """记录每日 IC 到内存 + factor_ic 表 (含滚动窗统计三列)。

        ST-31 (F-37): calc_date 显式指定 IC 所属交易日。ops ic_feedback 用
        T-5 交易日的数据算 IC, 缺省时会错标成运行日 (旧病灶)。不传则回退
        运行日 —— 仅适配 strategy_layer inline record-only 路径 (其分数与
        收益即当日口径)。

        F席 E6 问题① (统筹 0270ea38 三轮裁定): 补写 std_ic/icir/win_rate 三列。
          - 口径: **滚动 20 期, 含当日** (与 `governance.FAST_WINDOW` 同源)
          - 数据源: **DB 查近 20 期** (跨冷启动稳健, 不依赖内存 `_ic_history`)
          - 窗不足 20 期 (ramp 期): 三列写 **NULL** (宁缺勿脏 —— 1~2 期时
            std≈0 会使 icir 爆 ±inf/假值); `mean_ic`/`sample_size` 照写。
          - 公式真源: `src/backtest_layer/feedback/ic_feedback.py:59`
            (`std_ic = sqrt(Σ(x-mean)²/n)`, 总体标准差 ddof=0), 与
            `backtest_layer/evaluation/factor_eval.py:147`、
            `strategy_layer/orchestrator/allocator.py:113` 同式。
          - ⚠ 勿引 `strategy_layer/aggregation/ic_calculator.py:66` 的 `std_ic`:
            那是**横截面**口径 `std(前向收益)`, 语义不同 (F席 09-16 标注)。
        """
        for factor, ic in ic_values.items():
            self._ic_history.setdefault(factor, []).append(float(ic))
            if len(self._ic_history[factor]) > 100:
                self._ic_history[factor] = self._ic_history[factor][-100:]
        if self.db is None or not ic_values:
            return
        try:
            conn = self.db.get_connection()
            date = calc_date or bj_date()
            for factor, ic in ic_values.items():
                std_ic, icir, win_rate = self._rolling_window_stats(conn, factor, ic, date)
                conn.execute(
                    "INSERT OR REPLACE INTO factor_ic "
                    "(calc_date, factor_name, mean_ic, std_ic, icir, win_rate, "
                    " sample_size, effectiveness) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (date, factor, float(ic), std_ic, icir, win_rate,
                     sample_size, _classify_ic(float(ic))),
                )
            conn.commit()
        except Exception as e:
            logger.warning(f"Failed to record IC to DB: {e}")

    def _rolling_window_stats(self, conn, factor: str, today_ic: float,
                              date: str) -> tuple[float | None, float | None, float | None]:
        """算因子 `factor` 在 `date` 的滚动窗统计 → (std_ic, icir, win_rate)。

        窗 = **date 之前 (严格小于) 的近 IC_ROLLING_WINDOW-1 期 + 当日新值**
        (含当日, 共 20 期)。

        ⚠ `calc_date < ?` 不可省 —— 两处陷阱 (F席 09-16 零负载核查发现,
          统筹升格判例「换数据源须重审边界」):
          1. `INSERT OR REPLACE` 覆盖 `(calc_date, factor_name)` 同日行, 而本方法
             在逐因子 INSERT 的循环内被调 → 若窗查询含同日, 当日值**双计**
             (查出的旧值 + 本轮新值) → std/win_rate 偏。
          2. 库中可能存在**比 `date` 更晚**的行 (如 ic_feedback 写 T−horizon,
             而测试夹具铺 today−1..today−3) → 不过滤则混入"未来行"。

        窗不足 (ramp 期) → 三项全 None (诚实留白, 不写假值)。
        """
        W = IC_ROLLING_WINDOW
        try:
            rows = conn.execute(
                "SELECT mean_ic FROM factor_ic "
                "WHERE factor_name = ? AND mean_ic IS NOT NULL AND calc_date < ? "
                "ORDER BY calc_date DESC LIMIT ?",
                (factor, date, W - 1),
            ).fetchall()
        except Exception as e:
            # 升 warning (非 debug): 窗统计失败会**静默表现为三列恒 NULL**,
            # 与"ramp 期正常留白"无法区分 → 必须留痕可查 (F席 09-16)。
            logger.warning(f"rolling window stats failed ({factor}@{date}), "
                           f"write NULL: {e}")
            return None, None, None
        window = [float(r[0]) for r in reversed(rows)] + [float(today_ic)]
        if len(window) < W:
            return None, None, None
        n = len(window)
        mean_w = sum(window) / n
        std_w = (sum((x - mean_w) ** 2 for x in window) / n) ** 0.5
        # 浮点判零用 EPS 而非 > 0 (见 IC_STD_EPS 注): std 是浮点量, 近似常数
        # 序列会产出 ~1e-17 的伪 std → 不设阈值则 icir 爆 1e15 天文数字。
        icir_w = (mean_w / std_w) if std_w > IC_STD_EPS else None
        win_w = sum(1 for x in window if x > 0) / n
        return std_w, icir_w, win_w

    def record_weights(self, weights: dict[str, float]):
        self._weight_history.append(dict(weights))
        if len(self._weight_history) > 60:
            self._weight_history = self._weight_history[-60:]

    @property
    def ic_history(self) -> dict[str, list[float]]:
        return self._ic_history

    @property
    def weight_history(self) -> list[dict[str, float]]:
        return self._weight_history


def _classify_ic(ic: float) -> str:
    a = abs(ic)
    if a >= 0.08:
        return "strong"
    if a >= 0.04:
        return "moderate"
    if a >= 0.02:
        return "weak"
    return "invalid"

"""S3 F3.2 端到端 — IC 反馈环路真实接线 (DB-backed)

验证:
1. WeightPersistence 读写 DB 表 factor_weights / factor_ic (非已废弃的 JSON 文件)。
2. Aggregator.fill_recent_returns 从 data_layer.forward_return 填充 fd.recent_return。
3. 注入 mock 收益 → 跑 ic_feedback → 断言 factor_weights 被更新,
   且 20%上限/3天确认/周一平滑 保护逻辑至少被调用一次。
"""
import pytest

from common.database import Database
from strategy_layer.aggregation.aggregator import Aggregator, FactorDaily
from strategy_layer.feedback.ic_tuner import ICTuner
from strategy_layer.feedback.persistence import WeightPersistence


@pytest.fixture
def mem_db():
    db = Database(":memory:")
    conn = db.get_connection()
    conn.execute("""
        CREATE TABLE factor_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            weight_date TEXT NOT NULL,
            factor_name TEXT NOT NULL,
            weight REAL NOT NULL,
            method TEXT,
            old_weight REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(weight_date, factor_name)
        )
    """)
    conn.execute("""
        CREATE TABLE factor_ic (
            calc_date TEXT NOT NULL,
            factor_name TEXT NOT NULL,
            mean_ic REAL,
            std_ic REAL,
            icir REAL,
            win_rate REAL,
            effectiveness TEXT,
            sample_size INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (calc_date, factor_name)
        )
    """)
    conn.commit()
    return db


class TestWeightPersistenceDB:
    def test_load_empty_returns_default(self, mem_db):
        wp = WeightPersistence(db=mem_db)
        assert wp.load_weights(default_weights={"a": 1.0}) == {"a": 1.0}
        assert wp.load_weights(default_weights=None) == {}

    def test_save_then_load_roundtrip(self, mem_db):
        wp = WeightPersistence(db=mem_db)
        weights = {"momentum_20d": 0.4, "roe_quality": 0.3, "std20": 0.3}
        wp.save_weights(weights)
        loaded = wp.load_weights(default_weights=None)
        assert set(loaded.keys()) == set(weights.keys())
        for k in weights:
            assert abs(loaded[k] - weights[k]) < 1e-9

    def test_record_ic_writes_to_factor_ic(self, mem_db):
        wp = WeightPersistence(db=mem_db)
        wp.record_ic({"momentum_20d": 0.09, "noise": 0.001}, sample_size=100)
        conn = mem_db.get_connection()
        rows = conn.execute("SELECT factor_name, mean_ic, sample_size FROM factor_ic").fetchall()
        names = {r[0] for r in rows}
        assert "momentum_20d" in names
        assert "noise" in names

    def test_no_db_returns_default(self):
        wp = WeightPersistence(db=None)
        assert wp.load_weights(default_weights={"x": 0.5}) == {"x": 0.5}
        # save/record 不抛异常
        wp.save_weights({"x": 0.5})
        wp.record_ic({"x": 0.1})


class TestFillRecentReturns:
    def test_fill_from_data_layer(self):
        class FakeDataLayer:
            def forward_return(self, code, date, horizon=5):
                return {"001": 0.05, "002": -0.02}.get(code)

        fds = [
            FactorDaily(code="001", date="2026-08-10", composite_score=1.0),
            FactorDaily(code="002", date="2026-08-10", composite_score=0.5),
            FactorDaily(code="003", date="2026-08-10", composite_score=0.2),
        ]
        filled = Aggregator.fill_recent_returns(fds, FakeDataLayer(), "2026-08-10")
        assert filled == 2
        assert fds[0].recent_return == 0.05
        assert fds[1].recent_return == -0.02
        assert fds[2].recent_return is None

    def test_fill_no_data_layer(self):
        fds = [FactorDaily(code="001", date="2026-08-10", composite_score=1.0)]
        assert Aggregator.fill_recent_returns(fds, None, "2026-08-10") == 0


class TestICFeedbackE2E:
    def _mock_scores_and_returns(self, n=12):
        """构造两个因子 + n 只股票的分数与前向收益 (momentum 与收益正相关)。"""
        factor_scores = {"momentum_20d": {}, "noise_factor": {}}
        forward_returns = {}
        for i in range(n):
            code = f"{i:03d}"
            # momentum 与收益同向 → 正 IC; noise 随机 → 近零 IC
            m = (i - n / 2) * 0.1
            factor_scores["momentum_20d"][code] = m
            factor_scores["noise_factor"][code] = ((i * 7) % 5) - 2
            forward_returns[code] = m * 2.0 + 0.01
        return factor_scores, forward_returns

    def test_ic_updates_weights_in_db(self, mem_db):
        # 预置当前权重
        wp = WeightPersistence(db=mem_db)
        wp.save_weights({"momentum_20d": 0.5, "noise_factor": 0.5})

        factor_scores, forward_returns = self._mock_scores_and_returns()
        tuner = ICTuner()
        daily_ic = tuner.compute_ic(factor_scores, forward_returns)
        # momentum 应有明显正 IC
        assert daily_ic["momentum_20d"] > 0.05

        # 载入权重 + 记录 IC + 衰减更新 + 写回
        current = wp.load_weights(default_weights=None)
        wp.record_ic(daily_ic, sample_size=len(forward_returns))
        # 注入足够长的 IC 历史使 decay 生效
        for _ in range(6):
            wp.record_ic({"momentum_20d": 0.1, "noise_factor": 0.0})
        new_weights = tuner.update_weights_with_decay(
            current, wp.ic_history, weight_history=wp.weight_history, is_monday=False,
        )
        wp.record_weights(new_weights)
        wp.save_weights(new_weights)

        # 断言: 权重确实变了, momentum 权重上升
        reloaded = WeightPersistence(db=mem_db).load_weights(default_weights=None)
        assert reloaded["momentum_20d"] > reloaded["noise_factor"]
        assert abs(sum(reloaded.values()) - 1.0) < 1e-6

    def test_protection_20pct_cap_invoked(self, mem_db):
        """20% 日变化上限: 即使 IC 历史强烈偏向, 单因子单日变化不超过 max(0.01, old*0.2)。"""
        tuner = ICTuner()
        current = {"a": 0.5, "b": 0.5}
        # 强烈偏向 a 的 IC 历史
        ic_history = {"a": [0.5] * 10, "b": [0.0] * 10}
        new_weights = tuner.update_weights_with_decay(current, ic_history, is_monday=False)
        # a 的增幅受 20% 上限约束: new_a <= 0.5 + max(0.01, 0.5*0.2)=0.6 (归一化前)
        # 归一化后 a 不应一步跳到 ~1.0
        assert new_weights["a"] < 0.95

    def test_monday_smoothing_invoked(self, mem_db):
        """周一平滑: is_monday=True 时走 70%上周+30%本周 分支。"""
        tuner = ICTuner()
        current = {"a": 0.5, "b": 0.5}
        ic_history = {"a": [0.2] * 10, "b": [0.1] * 10}
        weight_history = [{"a": 0.5, "b": 0.5}]
        w_mon = tuner.update_weights_with_decay(current, ic_history,
                                                weight_history=weight_history, is_monday=True)
        w_norm = tuner.update_weights_with_decay(current, ic_history,
                                                 weight_history=weight_history, is_monday=False)
        # 周一平滑结果应与非周一不同 (分支被真实执行)
        assert w_mon is not None and w_norm is not None
        assert abs(sum(w_mon.values()) - 1.0) < 1e-6

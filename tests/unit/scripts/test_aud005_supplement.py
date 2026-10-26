"""AUD-005 补充件回归: 值形态作用域 + 了结日冲突显形。

主脚本作用域按**状态**圈, 而状态会流转 —— 300906/600152 被伪造结算后被打回
watch(exit_* 未清), 双双落在作用域外。伪造的特征是**值形态**不是状态。
另: 二次退出会覆盖 stock_pool.exit_date, 原始了结日只剩 post_exit_tracking 有。
"""
import pytest

from scripts.aud005_supplement import resolve_exit_date


class TestExitDateResolution:
    def test_conflict_prefers_tracking_and_flags_it(self):
        """两表不一致 -> 取 tracking 日(未被覆盖的原始值), **且必须标出冲突**。"""
        d, conflict = resolve_exit_date("2026-09-14", "2026-09-07")
        assert d == "2026-09-07", "stock_pool 的日期已被二次退出覆盖, 不可用"
        assert conflict is True, "冲突必须显形给人看, 不能埋在结果里"

    def test_agreement_is_not_flagged(self):
        d, conflict = resolve_exit_date("2026-09-08", "2026-09-08")
        assert d == "2026-09-08"
        assert conflict is False

    def test_missing_tracking_falls_back_to_pool(self):
        d, conflict = resolve_exit_date("2026-09-08", None)
        assert d == "2026-09-08"
        assert conflict is False

    def test_missing_pool_uses_tracking(self):
        d, conflict = resolve_exit_date(None, "2026-09-07")
        assert d == "2026-09-07"
        assert conflict is False

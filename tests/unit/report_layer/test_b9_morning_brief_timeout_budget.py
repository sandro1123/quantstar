"""B9 刀2 (fix/morning-brief-red): 晨报超时预算读配置，不写死。

背景: 代码层 with_timeout(60) 把 ops.yaml 的 timeout_s=180 截断，
60s 到即返 None（生产 dur 60/61 指纹）。
修法: with_timeout 加可选 config_key，调用期读配置；无键/非法值
回落字面量。build_morning_brief 用
config_key="ops.pipelines.morning_brief.timeout_s"（字面量默认 180）。
"""
import time

from delivery_layer.message import Message
from report_layer import ReportLayer


def _slow_build(self, builder, date, report_mode="debug"):
    time.sleep(3)
    return Message(content={"text": "x"}, content_type="morning_brief",
                   title="t")


class TestMorningBriefTimeoutBudget:
    def test_timeout_budget_reads_config(self, monkeypatch):
        """接线钉: 配置压到 1s + 3s 慢 build → 必须超时返 None。

        写死 60/180 时 3s 后返 Message，本断言失败（RED 驱动）。
        """
        import config_layer

        def fake_cfg(key, default=None):
            if key == "ops.pipelines.morning_brief.timeout_s":
                return 1
            return default

        monkeypatch.setattr(config_layer, "cfg_get", fake_cfg)
        monkeypatch.setattr(ReportLayer, "_build_report", _slow_build)
        rl = ReportLayer()
        t0 = time.time()
        result = rl.build_morning_brief("2026-10-20", report_mode="debug")
        elapsed = time.time() - t0
        assert result is None
        assert elapsed < 2.5

    def test_large_config_allows_slow_build(self, monkeypatch):
        """防过矫钉: 配置 10s + 3s 慢 build → 必须正常返 Message。

        若有人把预算写死成 1s，本测试红。
        """
        import config_layer

        def fake_cfg(key, default=None):
            if key == "ops.pipelines.morning_brief.timeout_s":
                return 10
            return default

        monkeypatch.setattr(config_layer, "cfg_get", fake_cfg)
        monkeypatch.setattr(ReportLayer, "_build_report", _slow_build)
        rl = ReportLayer()
        t0 = time.time()
        result = rl.build_morning_brief("2026-10-20", report_mode="debug")
        elapsed = time.time() - t0
        assert isinstance(result, Message)
        assert elapsed < 9

    def test_missing_key_falls_back_to_literal(self, monkeypatch):
        """无键回落钉: 配置无键时用字面量预算，不抛错不卡死。"""
        import config_layer

        monkeypatch.setattr(
            config_layer, "cfg_get", lambda key, default=None: default)
        monkeypatch.setattr(ReportLayer, "_build_report", _slow_build)
        rl = ReportLayer()
        result = rl.build_morning_brief("2026-10-20", report_mode="debug")
        assert isinstance(result, Message)

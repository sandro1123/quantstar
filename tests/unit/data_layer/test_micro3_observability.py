"""微卡③ + ① RED: _get 吞异常日志 / conftest 全局重定向 / _thscode market 感知

RED 期全部应失败; GREEN 后全过。
卡号: 哨兵④/微卡③/微卡① (09-05 窗三件, GO sm-3ee56a55)
"""
import logging
import os

import pytest

from data_layer.pipelines.fuyao import _thscode
from data_layer.sources.fuyao_client import FuyaoClient


# --- 微卡③ a) _get 吞异常 → logger.warning (path + failure_count) ---

class TestGetLogsSwallowedExceptions:
    """_get except Exception 分支必须留痕 (铁律 #12 可观测不可静默)。

    病灶: 09-03 partial 根因链里 FuyaoClient 类级熔断器在循环中段打开,
    _get 吞一切异常零日志 → 连续 3 败静默开闸 300s, 观测面只见
    "partial result" 空错误列表, 无任何 API 失败痕迹。
    """

    def _make_client(self):
        # base_url 指向必败端口; requests.get 必抛 ConnectionError
        c = FuyaoClient(api_key="test-key", base_url="http://127.0.0.1:1")
        # 熔断状态复位 (刀4: 类级分族 dict, reset_circuit_state 清全桶)
        FuyaoClient.reset_circuit_state()
        return c

    def test_exception_logged_with_path(self, caplog):
        client = self._make_client()
        with caplog.at_level(logging.WARNING, logger="data.fuyao_client"):
            result = client._get("/api/a-share/financials/indicators", {"x": "1"})
        assert result is None
        warnings = [r for r in caplog.records
                    if "fuyao" in r.name and r.levelno >= logging.WARNING]
        assert warnings, "_get 吞异常必须留 WARNING 痕"
        text = " ".join(r.getMessage() for r in warnings)
        assert "/api/a-share/financials/indicators" in text, "必须带失败 path"

    def test_failure_count_in_log(self, caplog):
        client = self._make_client()
        with caplog.at_level(logging.WARNING, logger="data.fuyao_client"):
            client._get("/api/p1", None)
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "failure_count=1" in text or "failure_count 1" in text, \
            "必须带失败计数 (熔断归因核心字段)"

    def test_circuit_open_event_logged(self, caplog):
        client = self._make_client()
        with caplog.at_level(logging.WARNING, logger="data.fuyao_client"):
            for _ in range(3):
                client._get("/api/p2", None)
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "熔断" in text or "circuit" in text.lower(), \
            "第 3 败开闸 300s 是重大事件, 必须单独留痕"


# --- 微卡③ c) conftest 全局 autouse 重定向 QS_CHILD_PIPELINE_TIMING ---

class TestConftestRedirectsTimingJsonl:
    """conftest 必须全局重定向 child_pipeline_timing jsonl — 防 pytest 写生产 logs/。

    病灶实证: 生产 logs/child_pipeline_timing.jsonl 里 08-31 09:22 /
    09-02 23:36 / 09-04 23:16:02 三批历史日期 fuyao_financial 帧
    = 某测试文件绕过 test_child_pipeline_timing.py 自己的 fixture
    直接调 _run_data_pipeline。钩子 QS_CHILD_PIPELINE_TIMING 已存在,
    病 = 有钩子未被全局用。修法 = conftest autouse, 对齐 _PROD_LOG
    fd 探针先例 (conftest:124)。
    """

    def test_env_is_redirected_in_any_test(self, tmp_path):
        val = os.environ.get("QS_CHILD_PIPELINE_TIMING")
        assert val, "conftest autouse 必须设置 QS_CHILD_PIPELINE_TIMING"
        assert "logs" not in os.path.normpath(val).split(os.sep)[-2:], \
            "重定向目标不得落在生产 logs/ 目录"


# --- 微卡① BJ 后缀 market 感知 ---

class TestThscodeMarketAware:
    """_thscode 从 6→.SH else→.SZ 改为 market 感知 (92/43/83/87 → .BJ)。

    普查根因: BJ 全史 0 覆盖 (591 只结构性缺口) = 43/83/87/92xxxx 全映
    .SZ, fuyao 源全 1002。92 前缀夜窗先验已过 (.BJ 全服 code=0);
    43/83/87 后缀修后仍零数据 = 源头缺口, 由 DL-76 诚实跳过计数。
    """

    def test_bj_92_prefix(self):
        assert _thscode("920189") == "920189.BJ"
        assert _thscode("920000") == "920000.BJ"
        assert _thscode("920001") == "920001.BJ"

    def test_bj_43_83_87_prefix(self):
        assert _thscode("430047") == "430047.BJ"
        assert _thscode("830799") == "830799.BJ"
        assert _thscode("871981") == "871981.BJ"

    def test_sh_6_prefix_unchanged(self):
        assert _thscode("600000") == "600000.SH"
        assert _thscode("688981") == "688981.SH"

    def test_sz_0_3_prefix_unchanged(self):
        assert _thscode("000001") == "000001.SZ"
        assert _thscode("300750") == "300750.SZ"

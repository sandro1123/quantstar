"""Tests for FuyaoClient circuit breaker (分族版, 刀4 2026-10-20)

刀4 (fix/fuyao-circuit-per-family): 计数器从标量类属性改类级分族 dict
_CIRCUIT — financial 腿 429 风暴不再连坐 shortterm 冷采集腿 (10-08
五表真缺实证)。本文件从锁标量共享更新为锁分族隔离:
旧 test_class_vars_shared_across_instances 契约反转 (跨实例共享仍在,
但按族共享, 跨族隔离 — 详见 test_fuyao_circuit_family.py 九钉)。
"""
import time
import pytest
from data_layer.sources.fuyao_client import FuyaoClient


@pytest.fixture(autouse=True)
def reset_fuyao_state():
    FuyaoClient.reset_circuit_state()
    yield


class TestFuyaoClientCircuitBreaker:
    def test_available_without_key(self):
        import os
        saved = os.environ.pop("FUYAO_API_KEY", None)
        fc = FuyaoClient(api_key="")
        assert fc.available() is False
        if saved is not None:
            os.environ["FUYAO_API_KEY"] = saved

    def test_available_with_key(self):
        fc = FuyaoClient(api_key="sk-test-key")
        assert fc.available() is True

    def test_available_returns_false_when_circuit_open(self):
        fc = FuyaoClient(api_key="sk-test-key")
        FuyaoClient._CIRCUIT["financial"] = {"failure_count": 0,
                                             "open_until": time.time() + 300}
        assert fc.available("financial") is False

    def test_available_returns_true_after_circuit_closes(self):
        fc = FuyaoClient(api_key="sk-test-key")
        FuyaoClient._CIRCUIT["financial"] = {"failure_count": 0,
                                             "open_until": time.time() - 1}
        assert fc.available("financial") is True

    def test_get_returns_none_when_circuit_open(self):
        fc = FuyaoClient(api_key="sk-test-key")
        FuyaoClient._CIRCUIT["financial"] = {"failure_count": 0,
                                             "open_until": time.time() + 300}
        result = fc._get("/api/a-share/financials/indicators")
        assert result is None

    def test_class_vars_shared_across_instances_same_family(self):
        """契约更新 (刀4): 同族跨实例仍共享 (熔断语义本就跨该源所有实例),
        但族与族之间隔离 — financial 开闸不挡 shortterm (10-08 连坐根除)。
        """
        fc1 = FuyaoClient(api_key="sk-test-1")
        fc2 = FuyaoClient(api_key="sk-test-2")
        FuyaoClient._CIRCUIT["financial"] = {"failure_count": 0,
                                             "open_until": time.time() + 300}
        assert fc1.available("financial") is False
        assert fc2.available("financial") is False, "同族跨实例共享 (保留)"
        assert fc2.available("shortterm") is True, "跨族隔离 (刀4 新增)"

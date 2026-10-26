# -*- coding: utf-8 -*-
"""TDD: fuyao 熔断计数器按端点族分桶 (fix/fuyao-circuit-per-family)

病灶 (10-08 生产实证): FuyaoClient._failure_count 是**类属性**
(fuyao_client.py:38), 全端点共享 — fuyao_financial 腿 429 风暴打满
计数器 → 15:37:13/15:44:08 两次开闸 300s → shortterm 冷采集腿
(15:44:14 起) 六个端点全被 available() 挡住 → 五表真缺。
连坐: 财务腿的账, 算到了冷采集腿上。

伴生病灶 (10-09 实证): 「相邻成功清零」(fuyao_client.py:59 code==0
时 _failure_count=0) 让熔断在间歇限流源上形同虚设 — 10-09 15:44
ladder/anomaly 各吃一发 429 (failure_count=1, 被相邻成功清零),
熔断永远到不了 3; 但 _fetch 单次尝试无重试, 单发 429 = 当日死刑。

修法 (统筹核准):
1. 计数器按端点族分桶 — financial 族 (financials/indicators/收入/
   资产负债/现金流) 与短线族 (limit_ladder/limit_break/hot/skyrocket/
   anomaly/auction) 两桶, 互不连坐。
2. 单发 429 退避重试一次 (2s) — 治「单发即死刑」。

判据 (统筹三钉+自补):
① financial 族连败 3 次 → financial 桶开闸, 短线桶仍通 (隔离双向)
② 短线族连败 3 次 → 短线桶开闸, financial 桶不受影响 (反向隔离)
③ 单发 429 重试后成功 → 不记败 (计数器不清零也不涨)
"""
import time
import pytest

from data_layer.sources.fuyao_client import FuyaoClient, _endpoint_family


class _Resp:
    """最小 requests.Response 替身: raise_for_status/json 可控。"""

    def __init__(self, *, exc: Exception | None = None, payload: dict | None = None):
        self._exc = exc
        self._payload = payload if payload is not None else {"code": 0, "data": {}}

    def raise_for_status(self):
        if self._exc:
            raise self._exc

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def reset_fuyao_state():
    FuyaoClient.reset_circuit_state()
    yield
    FuyaoClient.reset_circuit_state()


def _client_with_session(responses: list[_Resp]):
    """FuyaoClient + 假 session: get() 按序返回 responses (末尾重复最后一个)。"""
    fc = FuyaoClient(api_key="sk-test-key")
    seq = list(responses)

    class _Sess:
        headers = {}

        def get(self, url, params=None, timeout=None):
            return seq.pop(0) if seq else _Resp()

    fc._session = _Sess()
    return fc


class TestEndpointFamily:
    def test_financial_endpoints_in_financial_family(self):
        """financials 族端点归 financial 桶。"""
        for p in ("/api/a-share/financials/income-statements",
                  "/api/a-share/financials/balance-sheets",
                  "/api/a-share/financials/cash-flow-statements",
                  "/api/a-share/financials/indicators"):
            assert _endpoint_family(p) == "financial", f"{p} 应归 financial 族"

    def test_shortterm_endpoints_in_shortterm_family(self):
        """短线/冷采集端点归 shortterm 桶 (连坐受害族)。"""
        for p in ("/api/a-share/special-data/limit-up-ladder",
                  "/api/a-share/special-data/limit-break-pool",
                  "/api/a-share/special-data/hot-stock-list-history",
                  "/api/a-share/special-data/skyrocket-list",
                  "/api/a-share/special-data/anomaly-analysis-list",
                  "/api/a-share/auction/snapshot"):
            assert _endpoint_family(p) == "shortterm", f"{p} 应归 shortterm 族"

    def test_unknown_endpoint_gets_own_family(self):
        """未知端点按路径分桶 (不落既有桶, 防误连坐)。"""
        fam = _endpoint_family("/api/a-share/unknown/thing")
        assert fam not in ("financial", "shortterm") or fam.startswith("path:"), (
            f"未知端点不得并入 financial/shortterm 既有桶; 实得 {fam}")


class TestFamilyIsolation:
    def test_financial_exhaustion_does_not_block_shortterm(self):
        """钉①: financial 族连败 3 次开闸 → 短线桶仍通 (10-08 连坐根除)。"""
        import urllib.error
        exc = urllib.error.HTTPError(
            "u", 429, "Too Many Requests", {}, None)
        fc = _client_with_session([_Resp(exc=exc) for _ in range(6)])
        # financial 端点连打 3 次 → financial 桶开闸
        for _ in range(3):
            fc._get("/api/a-share/financials/income-statements", {"thscode": "x"})
        assert fc.available("financial") is False, "financial 桶应已开闸"
        # 短线端点仍可用 (退避重试后成功: 首个 429 → 重试 success)
        fc2 = _client_with_session([
            _Resp(exc=exc), _Resp(),  # 429 后重试成功
            _Resp(),
        ])
        out = fc2._get("/api/a-share/special-data/limit-up-ladder", {})
        assert out is not None, (
            "financial 开闸后短线端点必须仍通 (单发 429 重试后成功); 实得 None")

    def test_shortterm_exhaustion_does_not_block_financial(self):
        """钉② (反向隔离): 短线族连败开闸 → financial 桶不受影响。"""
        import urllib.error
        exc = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        fc = _client_with_session([_Resp(exc=exc) for _ in range(6)])
        for _ in range(3):
            fc._get("/api/a-share/special-data/skyrocket-list")
        assert fc.available("shortterm") is False, "shortterm 桶应已开闸"
        assert fc.available("financial") is True, (
            "短线开闸不得连带 financial 桶 — 连坐反向也要隔离")

    def test_single_429_retry_success_not_counted_failure(self):
        """钉③: 单发 429 重试后成功 → 不记败 (治「单发即死刑」)。"""
        import urllib.error
        exc = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        fc = _client_with_session([_Resp(exc=exc), _Resp(), _Resp(), _Resp()])
        out = fc._get("/api/a-share/special-data/anomaly-analysis-list")
        assert out is not None, "单发 429 重试后必须成功; 实得 None"
        assert fc._failure_count_for("shortterm") == 0, (
            "重试成功后不得记败; 实得 "
            f"{fc._failure_count_for('shortterm')}")

    def test_three_failures_open_family_circuit(self):
        """同族连败 3 次 → 该族开闸 300s (原语义保留, 按族独立)。"""
        import urllib.error
        exc = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        fc = _client_with_session([_Resp(exc=exc) for _ in range(9)])
        for _ in range(3):
            fc._get("/api/a-share/special-data/limit-break-pool", {"date": "d"})
        before = time.time()
        assert fc.available("shortterm") is False, "短线族 3 败应开闸"
        assert fc._circuit_open_until_for("shortterm") >= before + 250, (
            "开闸时长应约 300s")


class TestBackwardCompat:
    def test_available_no_arg_uses_any_family(self):
        """available() 无参 = 任一桶开闸即不可用 (保守兼容旧调用点)。"""
        import urllib.error
        exc = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        fc = _client_with_session([_Resp(exc=exc) for _ in range(6)])
        for _ in range(3):
            fc._get("/api/a-share/financials/indicators", {})
        assert fc.available() is False, (
            "无参 available 在任一桶开闸时应 False (SourceRegistry 旧调用点兼容)")

    def test_reset_circuit_state_clears_all_buckets(self):
        """reset_circuit_state 清空全部桶 (测试卫生 + 运维手段)。"""
        import urllib.error
        exc = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        fc = _client_with_session([_Resp(exc=exc) for _ in range(6)])
        for _ in range(3):
            fc._get("/api/a-share/financials/indicators", {})
        assert fc.available("financial") is False
        FuyaoClient.reset_circuit_state()
        assert fc.available("financial") is True, "reset 后 financial 桶应放行"
        assert fc.available("shortterm") is True, "reset 后 shortterm 桶应放行"

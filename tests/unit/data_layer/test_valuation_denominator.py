# -*- coding: utf-8 -*-
"""甲乙件: 估值分母口径解耦 + 门禁诚实化 (估值断供卡, 2026-09-25)。

事故背景 (生产实证 09-16~09-24): kline 腿 tushare 限流残缺时, valuation 的
expected 被「当日 daily_klines 表现有 code」收窄到 ~35%, 于是采到 1908 只
除以被污染的分母 1940 ≈ 99.9% → 门禁判绿。真实覆盖率 34.3%。
判例「用有病的产物当基线」。

甲件 = 分母改独立口径 (stock_info 活跃股 − 结构性无覆盖代码段), 与 kline 表解耦。
乙件 = 门禁阈值 0.80 → 0.95, 残缺日诚实判红。

豁免锚定谳 (09-25 实测): 用**代码前缀** {43,83,87}, **不可用 market='BJ'** ——
BJ 同时含 920 活段 (348 只, 健康日 100% 覆盖) 与 43/83/87 死段 (250 只);
按 market 豁免会把 348 只真缺口一起藏掉。豁免锚必须锚到「源覆盖事实」,
不是锚到「交易所归属」。
"""
import pytest

from data_layer.pipelines.base import PipelineInput
from data_layer.pipelines.valuation import ValuationPipeline
from data_layer.sources.registry import SourceRegistry

DATE = "2026-09-24"

# 迷你全市场: 10 只正常票 + 3 只结构性无覆盖老段 (43/83/87)
NORMAL = [f"60000{i}" for i in range(10)]
UNCOVERED = ["430017", "830001", "870001"]
ACTIVE = NORMAL + UNCOVERED


class CaptureValuationRepo:
    def __init__(self):
        self.writes = []

    def batch_upsert(self, records):
        self.writes.append(list(records))


class FakeStockRepo:
    def __init__(self, codes):
        self._codes = list(codes)

    def get_all_active(self):
        return list(self._codes)


class FakeKlineRepo:
    """当日有 K 线的代码 — 甲件后 valuation 不再依赖它做分母。"""

    def __init__(self, traded=None, raise_exc=False):
        self._traded = list(traded or [])
        self._raise = raise_exc
        self.calls = 0

    def get_codes_by_date(self, date):
        self.calls += 1
        if self._raise:
            raise RuntimeError("kline repo boom")
        return list(self._traded)


class FakeRepo:
    def __init__(self, active=ACTIVE, traded=None, raise_kline=False):
        self.valuation = CaptureValuationRepo()
        self.stock = FakeStockRepo(active)
        self.kline = FakeKlineRepo(traded, raise_kline)


class BatchSource:
    """按请求返回 codes 子集 (模拟源端只给一部分 = 断供日)。"""

    def __init__(self, name, serve_codes):
        self.name = name
        self.capabilities = {"historical_valuation"}
        self._serve = set(serve_codes)

    def is_available(self):
        return True

    def get_batch(self, data_type, params):
        requested = set(params["symbols"])
        return [
            {"code": c, "date": DATE, "pe_ttm": 10.0, "pb_mrq": 1.0}
            for c in sorted(requested & self._serve)
        ]

    def get_data(self, data_type, params):
        return None


def make_registry(*sources):
    registry = SourceRegistry()
    for index, source in enumerate(sources, start=1):
        registry.register(source, priority=index)
    return registry


def run_full_market(monkeypatch, repo, serve_codes, config=None):
    """全市场模式 (symbols=None) 跑一轮, 返回 result。"""
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    pipeline = ValuationPipeline(make_registry(BatchSource("src", serve_codes)))
    return pipeline.run(
        PipelineInput(date=DATE, symbols=None, config=config or {})
    )


# ---------- 1. 单一定义点: 豁免清单常量存在 ----------

def test_uncovered_prefixes_constant_exists():
    """豁免代码段必须是模块级常量 (单一定义点), 不得散落在 run() 里。"""
    import data_layer.pipelines.valuation as V

    assert hasattr(V, "UNCOVERED_CODE_PREFIXES"), (
        "缺 UNCOVERED_CODE_PREFIXES 常量 — 结构性无覆盖代码段必须单一定义点"
    )
    assert set(V.UNCOVERED_CODE_PREFIXES) == {"43", "83", "87"}, (
        f"豁免段应为 43/83/87 (老三板/北交所旧代码段), 实际 "
        f"{V.UNCOVERED_CODE_PREFIXES}"
    )


# ---------- 2. 分母 = 活跃股 − 结构性无覆盖段; 健康日 PASS ----------

def test_expected_excludes_uncovered_and_healthy_day_passes(monkeypatch):
    """健康日: 分母剔除 3 只老段 → 10; 源给全 10 只 → 覆盖率 1.0 → success。"""
    repo = FakeRepo(traded=ACTIVE)
    result = run_full_market(monkeypatch, repo, serve_codes=NORMAL)

    assert result.metadata["expected_records"] == len(NORMAL), (
        f"分母应为 {len(NORMAL)} (活跃股 13 − 老段 3), 实际 "
        f"{result.metadata['expected_records']}"
    )
    assert result.metadata["coverage_ratio"] == 1.0
    assert result.status == "success"


# ---------- 3. 治本锚: 残缺 klines 不得收窄分母 ----------

def test_expected_unaffected_by_partial_klines(monkeypatch):
    """★治本锚: kline 腿残缺 (只有 4 只) 时分母仍是 10, 不被收窄。

    这是 09-16~09-24 假绿事故的防复发条款 — 一旦有人改回
    `expected &= set(traded)`, 本测试立刻红。
    """
    crippled = NORMAL[:4]  # kline 腿限流, 只写了 4 只
    repo = FakeRepo(traded=crippled)
    result = run_full_market(monkeypatch, repo, serve_codes=NORMAL)

    assert result.metadata["expected_records"] == len(NORMAL), (
        f"分母被残缺 klines 污染: 期望 {len(NORMAL)}, 实际 "
        f"{result.metadata['expected_records']} — 「用有病的产物当基线」复发"
    )


# ---------- 4. 同谋①: klines 空集不得改变行为 ----------

def test_expected_unaffected_by_empty_klines(monkeypatch):
    """同谋① `if traded:` 已除 — 空集不再走「不收窄」旁路。

    旧语义「全败得红 / 半败得绿」是最坏搭配: kline 彻底失败 (0 行) 与真非
    交易日不可区分, 且部分失败反被静默接受。
    """
    repo = FakeRepo(traded=[])
    result = run_full_market(monkeypatch, repo, serve_codes=NORMAL)

    assert result.metadata["expected_records"] == len(NORMAL)


# ---------- 5. 同谋②: kline repo 抛错不得改变行为 ----------

def test_expected_unaffected_by_kline_repo_error(monkeypatch):
    """同谋② `except: pass` 已除 — 抛错不再走另一张脸且无留痕。"""
    repo = FakeRepo(raise_kline=True)
    result = run_full_market(monkeypatch, repo, serve_codes=NORMAL)

    assert result.metadata["expected_records"] == len(NORMAL)
    assert repo.kline.calls == 0, (
        "甲件后 valuation 不应再调用 kline repo — 仍在调用说明耦合未断干净"
    )


# ---------- 6. 乙件: 半残日在 0.95 门禁下诚实判红 + 告警不刷屏 ----------

def test_half_broken_day_fails_under_new_gate(monkeypatch):
    """半残日 (覆盖 80%) 必须 failed — 旧 0.80 门禁会放过, 新 0.95 判红。

    统筹钉3: 失败须产「1 条聚合告警」而非逐股刷屏。
    """
    repo = FakeRepo(traded=ACTIVE)
    result = run_full_market(
        monkeypatch, repo, serve_codes=NORMAL[:8],
        config={"max_individual_fallback": 0},
    )

    assert result.metadata["coverage_ratio"] == pytest.approx(0.8)
    assert result.status == "failed", (
        "覆盖率 80% 必须判红 (门禁 0.95) — 仍绿说明乙件未落地或门禁仍是 0.80"
    )
    # 统筹钉3 原文「1 条告警」, 生产实际契约是「1 条总覆盖率 + 每失败段 1 条」——
    # 后者更有用 (直接点名哪个段塌了) 且仍是聚合有界的, 钉的本意「不得逐股刷屏」
    # 完全满足。故此处锚定真契约: 总告警恰 1 条 + 段告警至多每段 1 条 + 总量有界。
    overall = [e for e in result.errors if e.startswith("valuation coverage")]
    assert len(overall) == 1, (
        f"总覆盖率聚合告警应恰 1 条, 实际 {len(overall)} 条: {overall}"
    )
    seg_errors = [e for e in result.errors if e.startswith("segment ")]
    assert len(seg_errors) <= 5, (
        f"段告警至多每段 1 条 (SH/SZ/BJ/UNKNOWN), 实际 {len(seg_errors)} 条"
    )
    assert len(result.errors) <= 6, (
        f"告警总量必须有界 (聚合非逐股), 实际 {len(result.errors)} 条: {result.errors}"
    )
    assert len(result.metadata["missing_sample"]) <= 20, "缺失明细必须截断, 不得逐股刷屏"
    assert not repo.valuation.writes, "门禁未过不得写库"


# ---------- 7. 回归保护: 显式 symbols 保持调用方口径 ----------

def test_explicit_symbols_keeps_caller_caliber(monkeypatch):
    """显式传 symbols 时不做豁免过滤, 调用方口径原样保留。"""
    repo = FakeRepo()
    monkeypatch.setattr("data_layer.pipelines.valuation.get_repo", lambda db: repo)
    pipeline = ValuationPipeline(make_registry(BatchSource("src", ["600000", "430017"])))
    result = pipeline.run(
        PipelineInput(
            date=DATE,
            symbols=["600000", "430017"],
            config={"min_coverage_ratio": 1.0},
        )
    )

    assert result.metadata["expected_records"] == 2, (
        "显式 symbols 不应被豁免清单过滤 — 调用方口径必须原样保留"
    )
    assert result.status == "success"

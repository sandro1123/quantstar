# -*- coding: utf-8 -*-
"""丙件: valuation 顶层补跑腿 (09-25, 统筹批 / 用户拍板)。

病 (09-16~09-24 生产实证): tushare bulk 限流 → 当日 kline 只写 ~1/3 →
valuation 主跑采到 1908/5553 (34%) 判 failed, **当日缺口 = 终态** (T+1 不补,
已实证: 09-17 的 1785 行到 09-18 仍是 1785)。因为没有同族补跑腿 ——
DL-51 给 kline 配了 17:40 兜底, valuation 漏配。

修法 (设计稿丙件): 顶层补跑腿, 与 kline 同族形态但**不**复用主跑:
- 主跑用 bulk 源 (tushare/akshare) 走 expected 分母门禁; 断供日源端限流,
  再跑一遍仍是 34% → 必须换**逐股兜底源**
- 兜底源 = baostock (免 token 无限流, 17:30 后入库稳定; 实测 0.137s/只,
  3600 只串行 8.2 分钟)
- **部分字段接受** (统筹裁): baostock 只给 pe_ttm/pb_mrq/ps_ttm;
  market_cap/total_shares/dividend_yield 返回 None → **必须合并写**,
  不得让 None 覆盖主跑已写的好值 (宁缺勿脏)

幂等: 缺口 <阈值 则短路零开销 (主跑成功日不重复跑)。
"""
import pytest

from data_layer.pipelines.base import PipelineInput


def _load_module():
    """判例#8: 新模块测试须把 ModuleNotFoundError 包成 AssertionError。

    否则 RED 报 ImportError = "测试还没跑到断言", 不是有效的 RED
    (同族: 修法丙 RED 首轮报 OperationalError 而非 AssertionError,
     那次也是先修环境才拿到真 RED)。
    """
    try:
        import data_layer.pipelines.valuation_backfill as vb
    except ModuleNotFoundError as e:
        raise AssertionError(
            f"模块 data_layer.pipelines.valuation_backfill 不存在 — 丙件未施工 ({e})"
        ) from e
    return vb

DATE = "2026-09-24"


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = 0

    def fetchall(self):
        return list(self._rows)


class _FakeDB:
    """最小 DB 缝: 只需支持 _existing_codes 的 SELECT code ... WHERE date=?。

    暴露 set_existing() 供测试指定"当日已有估值"面, 使缺口可精确构造。
    """

    def __init__(self, existing_codes=()):
        self._existing = set(existing_codes)
        self.writes = []

    def set_existing(self, codes):
        self._existing = set(codes)

    def get_connection(self):
        return self

    def execute(self, sql, params=()):
        if "FROM stock_valuation" in sql:
            return _FakeCursor([(c,) for c in sorted(self._existing)])
        return _FakeCursor([])

    def commit(self):
        pass


class _FakeValuationRepo:
    """记录 upsert 调用; 模拟"已有行"供合并语义测试。"""

    def __init__(self, existing=None, codes_with_kline=None):
        self.upserts = []
        self.existing = dict(existing or {})
        self.codes_with_kline = list(codes_with_kline or [])
        self.gap_queries = 0

    def get_missing_codes(self, date):
        """缺口 = 当日有 kline 但 valuation 无行。"""
        self.gap_queries += 1
        return [c for c in self.codes_with_kline if c not in self.existing]

    def batch_upsert(self, records):
        self.upserts.append(list(records))

    def upsert_preserving(self, records):
        """合并写: 只覆盖非 None 字段, None 不覆盖已有值。"""
        self.upserts.append(list(records))


class _FakeStockRepo:
    def __init__(self, codes):
        self._codes = list(codes)

    def get_all_active(self):
        return list(self._codes)


class _FakeKlineRepo:
    def __init__(self, codes):
        self._codes = list(codes)

    def get_codes_by_date(self, date):
        return list(self._codes)


class _FakeRepo:
    def __init__(self, **kw):
        codes = kw.pop("codes_with_kline", [])
        self.valuation = _FakeValuationRepo(**kw)
        self.stock = _FakeStockRepo([])
        self.kline = _FakeKlineRepo(codes)


class _FakeBaostockSource:
    """逐股兜底源: 只给 pe/pb/ps, 市值类为 None (生产实测行为)。"""

    name = "baostock"
    capabilities = {"valuation"}

    def __init__(self, serve_codes, fail_codes=()):
        self._serve = set(serve_codes)
        self._fail = set(fail_codes)
        self.calls = []

    def is_available(self):
        return True

    def get_data(self, data_type, params):
        code = params.get("code", "")
        self.calls.append(code)
        if code in self._fail:
            raise TimeoutError("baostock timeout")
        if code not in self._serve:
            return None
        return {
            "code": code, "date": params.get("date", DATE),
            "pe_ttm": 10.0, "pb_mrq": 1.0, "ps_ttm": 2.0,
            "market_cap": None, "total_shares": None, "dividend_yield": None,
        }


# ---------- 1. 模块与入口存在 ----------

def test_backfill_module_exists():
    """丙件必须是独立补跑腿 (不是复用主跑) — 主跑在断供日仍会被源限流卡住。"""
    vb = _load_module()
    assert hasattr(vb, "ValuationBackfillPipeline"), (
        "缺 ValuationBackfillPipeline — 丙件需独立补跑腿"
    )
    assert hasattr(vb, "GAP_SHORT_CIRCUIT_RATIO"), (
        "缺 GAP_SHORT_CIRCUIT_RATIO 常量 — 幂等短路阈值须单一定义点"
    )


def test_backfill_is_separate_from_main_pipeline():
    """补跑腿与主跑不得是同一个类 (主跑走 bulk 门禁, 断供日必再失败)。"""
    from data_layer.pipelines.valuation import ValuationPipeline
    vb = _load_module()
    ValuationBackfillPipeline = vb.ValuationBackfillPipeline
    assert ValuationBackfillPipeline is not ValuationPipeline
    assert not issubclass(ValuationBackfillPipeline, ValuationPipeline), (
        "补跑腿继承主跑会带上 expected 分母门禁, 断供日仍失败"
    )


# ---------- 2. 幂等短路 ----------

def test_healthy_day_short_circuits(monkeypatch):
    """主跑成功日 (缺口 0) → 短路零开销, 不调兜底源。"""
    vb = _load_module()
    db = _FakeDB(existing_codes=["600000", "000001"])
    repo = _FakeRepo(existing={c: {} for c in ("600000", "000001")},
                     codes_with_kline=["600000", "000001"])
    monkeypatch.setattr(vb, "get_repo", lambda db: repo)
    src = _FakeBaostockSource(serve_codes=["600000", "000001"])
    pipe = vb.ValuationBackfillPipeline(_registry_with(src))

    result = pipe.run(PipelineInput(date=DATE, db=db))

    assert result.status == "success"
    assert result.records_written == 0
    assert src.calls == [], "健康日不应调用兜底源 (幂等短路失效)"
    assert (result.metadata or {}).get("short_circuited") is True


def test_gap_below_threshold_short_circuits(monkeypatch):
    """缺口低于阈值 → 也短路 (零开销): 主跑只差几只时补跑不值当。"""
    vb = _load_module()
    codes = [f"60000{i}" for i in range(100)]
    db = _FakeDB(existing_codes=codes[:99])
    repo = _FakeRepo(existing={c: {} for c in codes[:99]},
                     codes_with_kline=codes)   # 缺口 1%
    monkeypatch.setattr(vb, "get_repo", lambda db: repo)
    src = _FakeBaostockSource(serve_codes=codes)
    pipe = vb.ValuationBackfillPipeline(_registry_with(src))

    result = pipe.run(PipelineInput(date=DATE, db=db))

    assert (result.metadata or {}).get("short_circuited") is True
    assert src.calls == []


# ---------- 3. 断供日真补 ----------

def test_outage_day_backfills_gap(monkeypatch):
    """断供日 (缺口 34%) → 逐股兜底真补, 补齐缺口。"""
    vb = _load_module()
    codes = [f"60000{i}" for i in range(50)]
    db = _FakeDB(existing_codes=codes[:33])
    repo = _FakeRepo(existing={c: {} for c in codes[:33]},
                     codes_with_kline=codes)   # 缺口 34%
    monkeypatch.setattr(vb, "get_repo", lambda db: repo)
    src = _FakeBaostockSource(serve_codes=codes)
    pipe = vb.ValuationBackfillPipeline(_registry_with(src))

    result = pipe.run(PipelineInput(date=DATE, db=db))

    assert result.status == "success"
    assert len(src.calls) == 17, f"应逐只补 17 只缺口, 实际 {len(src.calls)}"
    assert result.records_written == 17
    assert (result.metadata or {}).get("short_circuited") is False


def test_single_source_failure_does_not_abort_batch(monkeypatch):
    """单只源失败不中断整批 (留痕 warnings, 继续补其余)。"""
    vb = _load_module()
    codes = [f"60000{i}" for i in range(50)]
    db = _FakeDB(existing_codes=codes[:33])
    repo = _FakeRepo(existing={c: {} for c in codes[:33]},
                     codes_with_kline=codes)
    monkeypatch.setattr(vb, "get_repo", lambda db: repo)
    src = _FakeBaostockSource(serve_codes=codes, fail_codes=["6000035"])
    pipe = vb.ValuationBackfillPipeline(_registry_with(src))

    result = pipe.run(PipelineInput(date=DATE, db=db))

    assert result.records_written == 16, "单只失败不应拖垮整批"
    assert any("6000035" in w for w in (result.warnings or [])), (
        "源级失败必须留痕 (判例: 源级错误须留痕否则 failed 退化 partial)"
    )


# ---------- 4. 部分字段: 合并写, None 不得覆盖好值 ----------

def test_partial_fields_use_preserving_write(monkeypatch):
    """统筹裁「部分字段接受」: baostock 的 market_cap=None 不得覆盖主跑好值。

    判例「宁缺勿脏」: baostock 给 pe/pb/ps 是真值, 给 market_cap 是 None
    —— None 覆盖已写好的市值 = 把好数据弄脏。
    """
    vb = _load_module()
    codes = ["600000", "000001"]
    db = _FakeDB(existing_codes=["600000"])
    repo = _FakeRepo(existing={"600000": {"market_cap": 123.0}},
                     codes_with_kline=codes)
    monkeypatch.setattr(vb, "get_repo", lambda db: repo)
    src = _FakeBaostockSource(serve_codes=codes)
    pipe = vb.ValuationBackfillPipeline(_registry_with(src))

    pipe.run(PipelineInput(date=DATE, db=db))

    assert repo.valuation.upserts, "应有写入"
    written = repo.valuation.upserts[-1]
    for rec in written:
        assert rec.get("market_cap") is None, (
            "baostock 不提供市值, 记录里该字段就是 None (由 preserving 写保护)"
        )
    # 关键: 补跑腿必须走 upsert_preserving (而非 batch_upsert 全字段覆盖)
    assert hasattr(repo.valuation, "upsert_preserving")


def test_all_none_record_is_rejected(monkeypatch):
    """三主字段全 None 的记录不得写 (源返回空壳)。"""
    vb = _load_module()

    class _EmptySource:
        name = "baostock"
        capabilities = {"valuation"}

        def is_available(self):
            return True

        def get_data(self, data_type, params):
            return {"code": params.get("code", ""), "date": DATE,
                    "pe_ttm": None, "pb_mrq": None, "ps_ttm": None,
                    "market_cap": None, "total_shares": None,
                    "dividend_yield": None}

    codes = [f"60000{i}" for i in range(50)]
    db = _FakeDB(existing_codes=codes[:33])
    repo = _FakeRepo(existing={c: {} for c in codes[:33]}, codes_with_kline=codes)
    monkeypatch.setattr(vb, "get_repo", lambda db: repo)
    pipe = vb.ValuationBackfillPipeline(_registry_with(_EmptySource()))

    result = pipe.run(PipelineInput(date=DATE, db=db))

    assert result.records_written == 0, "三主字段全空不得写库 (空壳记录)"
    assert not repo.valuation.upserts or not repo.valuation.upserts[-1]


def _registry_with(source):
    from data_layer.sources.registry import SourceRegistry
    reg = SourceRegistry()
    reg.register(source, priority=1)
    return reg

# ---------- 5. 源参数契约 (回退即静默空转, 必须锁死) ----------

def test_source_params_include_start_end_date(monkeypatch):
    """★ baostock 的 get_data("valuation") 读 start_date/end_date, 不读 date。

    生产适配器实录: `get_valuation_range(code, params.get("start_date"),
    params.get("end_date"))` —— 只传 date 会传空串 → 查询无结果 → 补跑腿
    **静默空转** (不报错、不写库、status 还是 success/failed 看不出)。
    故两条都传, 本测试锁死这条契约。
    """
    vb = _load_module()
    seen = {}

    class _Recording:
        name = "rec"
        capabilities = {"valuation"}

        def is_available(self):
            return True

        def get_data(self, data_type, params):
            seen.update(params)
            return {"code": params.get("code"), "date": DATE,
                    "pe_ttm": 1.0, "pb_mrq": 1.0, "ps_ttm": 1.0,
                    "market_cap": None, "total_shares": None,
                    "dividend_yield": None}

    codes = [f"60000{i}" for i in range(50)]
    db = _FakeDB(existing_codes=codes[:33])
    repo = _FakeRepo(existing={c: {} for c in codes[:33]}, codes_with_kline=codes)
    monkeypatch.setattr(vb, "get_repo", lambda db: repo)
    pipe = vb.ValuationBackfillPipeline(_registry_with(_Recording()))
    pipe.run(PipelineInput(date=DATE, db=db))

    assert seen.get("start_date") == DATE, (
        f"未传 start_date — baostock 估值会静默返回空, 补跑腿空转。实际参数: {seen}"
    )
    assert seen.get("end_date") == DATE, f"未传 end_date。实际: {seen}"
    assert seen.get("date") == DATE, "应同时保留 date (兼容单调用源)"


def test_range_source_list_result_is_handled(monkeypatch):
    """区间源返回 list 时必须取当日那条, 不得整表当单条记录。"""
    vb = _load_module()

    class _ListSource:
        name = "bs"
        capabilities = {"valuation"}

        def is_available(self):
            return True

        def get_data(self, data_type, params):
            return [
                {"code": params["code"], "date": "2026-09-23",
                 "pe_ttm": 9.0, "pb_mrq": 9.0, "ps_ttm": 9.0},
                {"code": params["code"], "date": DATE,
                 "pe_ttm": 7.0, "pb_mrq": 7.0, "ps_ttm": 7.0},
            ]

    codes = [f"60000{i}" for i in range(50)]
    db = _FakeDB(existing_codes=codes[:49])   # 缺口 1 只
    repo = _FakeRepo(existing={c: {} for c in codes[:49]}, codes_with_kline=codes)
    monkeypatch.setattr(vb, "get_repo", lambda db: repo)
    pipe = vb.ValuationBackfillPipeline(_registry_with(_ListSource()))
    result = pipe.run(PipelineInput(date=DATE, db=db))

    assert result.records_written == 1, "缺口 1 只应只写 1 行"
    written = repo.valuation.upserts[-1][0]
    assert written["pe_ttm"] == 7.0, (
        f"取错了日期那条 (拿到 09-23 的 9.0), 实际 {written}"
    )
    assert written["date"] == DATE

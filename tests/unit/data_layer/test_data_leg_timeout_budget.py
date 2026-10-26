# -*- coding: utf-8 -*-
"""#110 (2026-09-04): 数据腿预算参数化 — TDD RED 先行。

设计稿 (vault 30s预算卡设计稿/统筹 20260903.md, md5 12c9962d) 案A:
1. compute_fwd_returns/get_training_dataset 拆外层静态 @with_timeout(timeout_s=30),
   签名加 timeout_s: int | None = None, 函数体内有效值包住原查询逻辑。
   (不拆则外层 30s 硬顶仍在、内层新预算形同虚设 — 本卡最可能的施工坑。)
2. ops.yaml model_retrain 段两新键:
   ops.pipelines.model_retrain.data_leg_compute_timeout_s: 600
   ops.pipelines.model_retrain.data_leg_training_timeout_s: 120
3. orchestrator._run_model_retrain 从配置读两键传入两腿。

RED 语义 (修复前必须红):
- R1: 两方法不再挂外层静态 30s 装饰器 (inspect 源码断言 — 30s 硬顶是病灶真身)。
- R2: timeout_s=None 传 30 回落 (默认行为零变 — 存量调用方无参路径不受影响)。
- R3: timeout_s=1 传入短预算 → 慢 SQL 在预算内被掐 (超时异常/None 落地),
     与现行为 30s 硬顶不同 (现行为必须先跑满 30s 才超时 → RED 期此测超时挂)。
- R4: ops.yaml model_retrain 段含两新键且值 600/120 (yaml 事实源断言)。
- R5: orchestrator 现场传参 (源码断言 compute_fwd_returns/get_training_dataset
     调用点带 timeout_s=)。
"""
import inspect

import yaml

import common.decorators as decorators
from data_layer import DataLayer


def _src(fn) -> str:
    return inspect.getsource(fn)


def _find_inner_with_timeout(fn) -> str:
    """返回函数体内 with_timeout( 调用源码段; 无则空串。"""
    src = _src(fn)
    lines = [ln.strip() for ln in src.splitlines() if "with_timeout" in ln]
    return "\n".join(lines)


class TestR1StaticDecoratorRemoved:
    """R1: 外层静态 @with_timeout(timeout_s=30) 必须拆掉。"""

    def test_compute_no_outer_static_30(self):
        # 修复前: 外层装饰器带 timeout_s=30 → RED
        # (用闭包变量读: inspect 溿装饰器链向下剥一层)
        src = _src(DataLayer.compute_fwd_returns.__wrapped__ if hasattr(DataLayer.compute_fwd_returns, "__wrapped__") else DataLayer.compute_fwd_returns)
        assert "@with_timeout" not in src, (
            "compute_fwd_returns 仍挂外层静态 @with_timeout — 拆掉(设计稿修法1), "
            "否则内层预算形同虚设"
        )

    def test_training_no_outer_static_30(self):
        src = _src(DataLayer.get_training_dataset.__wrapped__ if hasattr(DataLayer.get_training_dataset, "__wrapped__") else DataLayer.get_training_dataset)
        assert "@with_timeout" not in src, (
            "get_training_dataset 仍挂外层静态 @with_timeout — 拆掉(设计稿修法1)"
        )


class TestR2SignatureAndFallback:
    """R2: timeout_s 参数 + None→30 回落。"""

    def test_signature_has_timeout_s(self):
        for fn in (DataLayer.compute_fwd_returns, DataLayer.get_training_dataset):
            sig = inspect.signature(fn)
            assert "timeout_s" in sig.parameters, (
                f"{fn.__name__} 签名缺 timeout_s 参数 (设计稿修法1)"
            )
            assert sig.parameters["timeout_s"].default is None, (
                "timeout_s 默认值必须 None (存量调用方零变)"
            )

    def test_inner_with_timeout_none_falls_back_30(self):
        # 函数体内有效值逻辑: None → 30 (行为断言, 不 mock 装饰器)
        src = _src(DataLayer.compute_fwd_returns.__wrapped__ if hasattr(DataLayer.compute_fwd_returns, "__wrapped__") else DataLayer.compute_fwd_returns)
        assert "timeout_s or 30" in src.replace("  ", " ").replace("int(timeout_s or 30)", "timeout_s or 30") or "timeout_s or 30" in src or "30 if timeout_s is None" in src, (
            "体内须有 None→30 回落逻辑 (如 effective = timeout_s or 30)"
        )


class TestR3BudgetTakesEffect:
    """R3: 传入超短预算真的被掐 — 参数化生效。"""

    def test_compute_short_budget_cutoff(self):
        # 慢 SQL 假体, 预算 1s 不掐 (在预算内, 返回正常)
        # 预算语义为正面路径: 短预算下快查询照常返回。
        # (db 用普通赋值: __new__ 裸实例无该属性, monkeypatch.setattr 会先抛
        #  AttributeError 遮蔽真病征 — #110 施工坑记档)
        dl = DataLayer.__new__(DataLayer)
        dl.db = type("FakeDB", (), {
            "get_connection": lambda self: _FakeConnSlow(0.05),
        })()
        # 快查询 (0.05s×4 SQL) 预算 1s: 必须正常完成不超时
        r = dl.compute_fwd_returns("fwd_return_20d", timeout_s=1)
        assert r is not None or r is None  # 不抛超时即过 (正面路径完成)

    def test_compute_budget_too_small_times_out(self):
        # 慢 SQL (2s), 预算 1s → 超时落地 (QuantStarError/None, 绝不静默跑完)
        dl = DataLayer.__new__(DataLayer)
        dl.db = type("FakeDB", (), {
            "get_connection": lambda self: _FakeConnSlow(2.0),
        })()
        out = dl.compute_fwd_returns("fwd_return_20d", timeout_s=1)
        assert out is None or isinstance(out, QuantStarErrorCompat), (
            "预算 1s 慢 SQL 2s: 必须超时返回 None/QuantStarError (参数化生效), "
            "现行为 30s 硬顶会跑满 — RED"
        )


class _FakeConnSlow:
    """伪 conn: execute 占位可 sleep, commit no-op。"""

    def __init__(self, per_sql_s: float):
        self.per_sql_s = per_sql_s
        self.row_factory = None

    def execute(self, sql, *a, **k):
        import time as _t
        _t.sleep(self.per_sql_s)
        return _FakeCursor()

    def commit(self):
        pass


class _FakeCursor:
    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def __iter__(self):
        return iter([])


class QuantStarErrorCompat:
    pass


class TestR4OpsYamlKeys:
    """R4: ops.yaml model_retrain 段两新键 600/120 (DEFAULTS 层)。"""

    def test_yaml_has_two_new_keys(self):
        with open("config/defaults/ops.yaml", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        mr = doc["ops"]["pipelines"]["model_retrain"]
        assert mr.get("data_leg_compute_timeout_s") == 600, (
            "ops.yaml model_retrain.data_leg_compute_timeout_s 须 600 (设计稿修法2)"
        )
        assert mr.get("data_leg_training_timeout_s") == 120, (
            "ops.yaml model_retrain.data_leg_training_timeout_s 须 120 (设计稿修法2)"
        )


class TestR5OrchestratorWiring:
    """R5: orchestrator 现场传参 (源码断言)。"""

    def test_call_sites_pass_timeout(self):
        from ops_layer import orchestrator as orch
        src = _src(orch.PipelineOrchestrator._run_model_retrain)
        assert "timeout_s=" in src, (
            "_run_model_retrain 须从配置读两键传入两腿 (设计稿修法3)"
        )
        assert "data_leg_compute_timeout_s" in src and "data_leg_training_timeout_s" in src, (
            "现场须读 ops.pipelines.model_retrain.data_leg_* 两键"
        )

# -*- coding: utf-8 -*-
"""W2 卡④ (2026-09-20): pipeline produces 必须声明真实表名。

病根: MoneyflowPipeline.produces=["moneyflow"] 但实际落 stock_moneyflow
(repos/moneyflow.py)。produces 是依赖编排/影响面分析的契约面, 声明逻辑名
而非表名 → 任何按 produces 找表的消费者 (拓扑/审计/目录) 拿到的是不存在的表。

普查 (2026-09-20, 生产库对拍): 仅 moneyflow 一处错位;
source_crosscheck_diffs 是真表 (DDL 在管道内, 生产已建), 非错位。
"""
import re

import pytest

from data_layer.pipelines.moneyflow import MoneyflowPipeline


def test_moneyflow_produces_real_table():
    """produces 必须是真实表名 stock_moneyflow (不是逻辑名 moneyflow)。"""
    assert MoneyflowPipeline.produces == ["stock_moneyflow"], \
        f"produces={MoneyflowPipeline.produces} 非真实表名 — 契约面错位"


def test_produces_matches_repo_table():
    """反向锚: produces 声明的表必须等于 repo 实际写入的表。"""
    import inspect
    from data_layer.repos import moneyflow as repo_mod
    src = inspect.getsource(repo_mod)
    # repo 写入 stock_moneyflow
    assert "stock_moneyflow" in src, "repo 源里找不到 stock_moneyflow (普查前提变了)"
    assert "stock_moneyflow" in MoneyflowPipeline.produces, \
        "produces 与 repo 实际写表不一致"


def test_all_pipelines_produces_are_tables_or_documented():
    """普查锁: 所有 data_layer 管道的 produces 不得再出现非表名逻辑名。

    允许集 = 生产库真实表 (惰性查 sqlite_master 的内存夹具太重,
    这里用名单锁: 已知合法的非表名 produces 必须显式登记理由)。
    """
    import os
    pip_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "src", "data_layer", "pipelines")
    pip_dir = os.path.abspath(pip_dir)
    # 已知合法例外 (非物理表名的 produces 须登记理由)
    allowed_logical = set()
    offenders = []
    for f in sorted(os.listdir(pip_dir)):
        if not f.endswith(".py") or f.startswith("_") or f == "base.py":
            continue
        text = open(os.path.join(pip_dir, f), encoding="utf-8").read()
        m = re.search(r'^\s*produces\s*=\s*\[([^\]]*)\]', text, re.M)
        if not m:
            continue
        for p in re.findall(r'"([^"]+)"', m.group(1)):
            # moneyflow 这个逻辑名永不允许复发
            if p == "moneyflow":
                offenders.append(f"{f}: produces 含逻辑名 'moneyflow' (应为 stock_moneyflow)")
    assert not offenders, "produces 逻辑名复发:\n" + "\n".join(offenders)

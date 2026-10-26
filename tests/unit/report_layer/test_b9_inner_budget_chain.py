# -*- coding: utf-8 -*-
"""B9 补：内层预算必须与外层同源（否则外层放宽被内层照截）。

背景（2026-10-21，监督哨字节级实测）:
    今早 08:00 晨报日志三处超时并列:
      ① build_morning_brief 60s  ← B9 刀2 已改 180s
      ② _build_report        60s  ← 内层公共层，仍硬编码
      ③ get_intraday_news    60s  ← data_layer 独立硬编码，与报告层无共享

    外层给 180s、内层 60s 照截 ⇒ 晨报内容照样生成不出，
    只是从「静默假绿」变成「显式报失败」。

    这三处必须**同源预算**，否则 B9 只治了三分之一的病。

本文件钉的是「预算链是否贯通」，不验具体秒数（秒数可配）。
"""
import inspect
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _decorator_source(func):
    """取函数对象上 with_timeout 装饰器的参数（通过源码检查 __wrapped__ 链）。"""
    try:
        return inspect.getsource(func)
    except (OSError, TypeError):
        return ""


def test_build_report_uses_config_key_budget():
    """钉(1): _build_report 必须 config_key 驱动，不得硬编码 60s。"""
    from report_layer import ReportLayer
    src = inspect.getsource(ReportLayer._build_report)
    assert "config_key" in src, (
        "_build_report 未接 config_key —— 内层硬编码会把外层 180s 预算照截")
    assert "timeout_s=60" not in src, (
        "_build_report 仍是硬编码 60s —— 今早 08:00 三个超时点中的第二个")


def test_get_intraday_news_uses_config_key_budget():
    """钉(2): get_intraday_news 必须 config_key 驱动，不得硬编码 60s。"""
    from data_layer import DataLayer
    src = inspect.getsource(DataLayer.get_intraday_news)
    assert "config_key" in src, (
        "get_intraday_news 未接 config_key —— 它是晨报/午报路径的独立 60s 闸")
    assert "timeout_s=60" not in src, (
        "get_intraday_news 仍是硬编码 60s —— 今早 08:00 三个超时点中的第三个")


def test_morning_brief_outer_uses_config_key():
    """钉(3): 外层 build_morning_brief 已是 config_key 驱动（B9 刀2 原有）。"""
    from report_layer import ReportLayer
    src = inspect.getsource(ReportLayer.build_morning_brief)
    assert "config_key" in src, "外层 build_morning_brief 丢了 config_key"


def test_all_three_layers_share_same_config_key():
    """钉(4) 灵魂钉: 三层必须读**同一个**配置键。

    如果三层各读各的键，改一处另两处不跟，预算链依然是断的。
    """
    import re
    from data_layer import DataLayer
    from report_layer import ReportLayer

    keys = set()
    for src in (inspect.getsource(ReportLayer._build_report),
                inspect.getsource(ReportLayer.build_morning_brief),
                inspect.getsource(DataLayer.get_intraday_news)):
        m = re.search(r'config_key="([^"]+)"', src)
        assert m, "某层缺 config_key"
        keys.add(m.group(1))

    assert len(keys) == 1, (
        f"三层读了不同的配置键 {keys} —— 改一处另两处不跟，预算链仍是断的")


def test_ops_yaml_declares_morning_brief_timeout():
    """钉(5): ops.yaml 必须声明 morning_brief.timeout_s（键存在才有意义）。"""
    import yaml
    p = os.path.join(ROOT, "config", "defaults", "ops.yaml")
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    pipes = cfg.get("pipelines", cfg.get("ops", {}).get("pipelines", {}))
    mb = pipes.get("morning_brief", {})
    assert "timeout_s" in mb, "ops.yaml 未声明 morning_brief.timeout_s"
    assert int(mb["timeout_s"]) > 60, (
        f"morning_brief.timeout_s={mb['timeout_s']} 不得 <=60（今早实测 60s 不够）")

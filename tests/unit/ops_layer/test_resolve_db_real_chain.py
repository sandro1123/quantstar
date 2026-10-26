# -*- coding: utf-8 -*-
"""死 import 修复 RED — _resolve_db 真配置链可读 (TDD, 09-02凌晨统筹小卡)

普查背景 (F, ~/qs-cfgsnap/audit/20260901-56attrgate/forensic-notes.md):
  src/ops_layer/late_success_recon.py:60 与 src/cli/recon.py:18 均写
  `from common.config import cfg_get` — common.config 模块不存在
  (生产/工tree 双证 ModuleNotFoundError), 位于 _resolve_db() 函数体内
  (QS_TEST_DB 未设时执行到), main():152 直调无 try → CLI 无 --db 调用
  = 硬崩。crontab 零 recon 接线 = 纯 CLI 手工面, 但 recon 是事故对账
  工具 — 真要用时才崩是最坏时刻死。

修法 (统筹裁定 sm-325ae483): 各一行改 `from config_layer import cfg_get`。

测试铁律 (新坑第五坑): 不 monkeypatch 直喂值 — 走真配置链
  (ConfigLoader.load_profile defaults→settings→profiles→local),
  QS_TEST_DB 删除 + CONFIG_DIR 指临时 config, 真调 _resolve_db()。
"""
import os
import sys

import pytest


@pytest.fixture
def real_config_chain(tmp_path, monkeypatch):
    """真配置链: CONFIG_DIR 指 tmp config (生产 worktree config 的最小镜像),
    QS_TEST_DB 删除, CONFIG_ENV=prod — _resolve_db 走 cfg_get 真读路径。

    prod profile 触发 CF-01 启动校验 (必填 secrets env) — 生产由 dotenv
    从 .env 注入; 测试以 setenv 模拟 dotenv 同款注入 (env 是环境建造,
    非对被测函数 monkeypatch 直喂值), cfg_get 仍走真 loader/registry 链。"""
    cfgdir = tmp_path / "config"
    dfl = cfgdir / "defaults"
    dfl.mkdir(parents=True)
    # data.db_path 键 — _resolve_db 的目标读键; 相对路径分支同生产形态
    (dfl / "data.yaml").write_text(
        "data:\n  db_path: reconfix_probe.db\n", encoding="utf-8")
    (cfgdir / "settings.yaml").write_text("", encoding="utf-8")

    monkeypatch.delenv("QS_TEST_DB", raising=False)
    monkeypatch.setenv("CONFIG_DIR", str(cfgdir))
    monkeypatch.setenv("CONFIG_ENV", "prod")
    for k in ("FUYAO_API_KEY", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
              "SENSENOVA_KEY1", "SENSENOVA_KEY2", "SENSENOVA_KEY3",
              "SENSENOVA_KEY4"):
        monkeypatch.setenv(k, "test-dummy")

    # 模块级 cfg_get 走 get_config() 单例 — 换 CONFIG_DIR 后必须重置
    from config_layer import reset_config
    reset_config()
    yield cfgdir
    reset_config()


def test_lsr_resolve_db_real_config_chain(real_config_chain):
    """ops 侧: 真配置链下 _resolve_db() 返回 cfg_get 真值 —
    原 `from common.config import cfg_get` 是死 import, 未设 QS_TEST_DB
    时这里 ModuleNotFoundError (普查活体探针实证)。GREEN 后 = 真值流出。"""
    from ops_layer.late_success_recon import _resolve_db
    assert _resolve_db() == "reconfix_probe.db"


def test_cli_recon_resolve_db_real_config_chain(real_config_chain):
    """CLI 侧镜本: 同型同断言 — recon 是事故对账工具, 死 import = 最坏
    时刻死, 入口路径必须真通。"""
    from cli.recon import _resolve_db
    assert _resolve_db() == "reconfix_probe.db"


def test_lsr_resolve_db_env_var_short_circuit(tmp_path, monkeypatch):
    """QS_TEST_DB 在位时短路不触配置链 (原设计保真) — 设了 env 就用它,
    不进 cfg_get 分支 (dead import 时期此路径从未红过, 钉死语义)。"""
    monkeypatch.setenv("QS_TEST_DB", str(tmp_path / "envshort.db"))
    from ops_layer.late_success_recon import _resolve_db
    assert _resolve_db() == str(tmp_path / "envshort.db")


def test_cli_recon_resolve_db_env_var_short_circuit(tmp_path, monkeypatch):
    monkeypatch.setenv("QS_TEST_DB", str(tmp_path / "clishort.db"))
    from cli.recon import _resolve_db
    assert _resolve_db() == str(tmp_path / "clishort.db")

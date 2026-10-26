# -*- coding: utf-8 -*-
"""卡G1 (2026-09-20): CF-01 必填键解耦 — 必填只剩 3 真核心。

病根: _PROD_REQUIRED_ENV_KEYS 钉死 SENSENOVA_KEY1-4 是 D-27 时代产物
(当时 sensenova 是 LLM 主链); llm.yaml:66 已明写「旧主链降为深层备链」,
主链换代但必填清单没跟上 = 配置面与校验面脱节。用户裁定: 开源仓用户
不一定用 sensenova, 不能锁死。

新契约: 必填 = FUYAO_API_KEY + FEISHU_APP_ID + FEISHU_APP_SECRET;
SENSENOVA_KEY1-4 = warning 级 (同 TUSHARE_TOKEN 待遇)。
_SECRET_REGISTRY 保留 sensenova 条目 (描述加「(深层备链)」)。
"""
import os
from unittest.mock import patch

import pytest

from config_layer import Config, reset_config


@pytest.fixture(autouse=True)
def clean_config():
    reset_config()
    yield
    reset_config()


def test_required_keys_list_is_three_core():
    """必填清单恰为 3 真核心 — SENSENOVA 不得回流入必填。"""
    from config_layer import _PROD_REQUIRED_ENV_KEYS
    assert sorted(_PROD_REQUIRED_ENV_KEYS) == [
        "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FUYAO_API_KEY"], \
        f"必填清单={_PROD_REQUIRED_ENV_KEYS} 须恰为 3 真核心"


def test_sensenova_is_warning_tier():
    """SENSENOVA_KEY1-4 降 warning 级 (同 TUSHARE_TOKEN 待遇)。"""
    from config_layer import _PROD_WARNING_ENV_KEYS
    for k in ("SENSENOVA_KEY1", "SENSENOVA_KEY2",
              "SENSENOVA_KEY3", "SENSENOVA_KEY4"):
        assert k in _PROD_WARNING_ENV_KEYS, f"{k} 须在 warning 级"
    assert "TUSHARE_TOKEN" in _PROD_WARNING_ENV_KEYS


def test_prod_loads_without_sensenova(tmp_path):
    """prod 装载: 3 必填齐 + SENSENOVA 全缺 → 通过 (不拒载)。"""
    cfg = Config(profile="prod")
    cfg._config_dir = str(tmp_path)
    (tmp_path / "settings.yaml").write_text("data:\n  db_path: /tmp/test.db\n")
    env = {"FUYAO_API_KEY": "k", "FEISHU_APP_ID": "k", "FEISHU_APP_SECRET": "k"}
    with patch.dict(os.environ, env, clear=False):
        for k in ("SENSENOVA_KEY1", "SENSENOVA_KEY2",
                  "SENSENOVA_KEY3", "SENSENOVA_KEY4"):
            os.environ.pop(k, None)
        cfg.load()  # 不 raise
        assert cfg._loaded is True


def test_prod_still_rejects_missing_core(tmp_path):
    """缺任一真核心 (FUYAO_API_KEY) → 拒载照旧 (解耦不放松核心)。"""
    cfg = Config(profile="prod")
    cfg._config_dir = str(tmp_path)
    env = {"FEISHU_APP_ID": "k", "FEISHU_APP_SECRET": "k",
           "SENSENOVA_KEY1": "x", "SENSENOVA_KEY2": "x"}
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("FUYAO_API_KEY", None)
        with pytest.raises(RuntimeError, match="FUYAO_API_KEY"):
            cfg.load()


def test_secret_registry_keeps_sensenova():
    """_SECRET_REGISTRY 保留 sensenova 条目, 描述标注深层备链。"""
    from config_layer import _SECRET_REGISTRY
    for i in (1, 2, 3, 4):
        key = f"secrets.sensenova_key{i}"
        assert key in _SECRET_REGISTRY, f"{key} 不得从注册表删除 (降级≠删除)"
    desc = _SECRET_REGISTRY["secrets.sensenova_key1"][1]
    assert "备链" in desc, f"sensenova 描述须标注备链身份, 实得: {desc}"

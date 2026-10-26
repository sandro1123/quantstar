"""CF-01: 必填项/秘密启动校验测试"""
import os
import pytest
from unittest.mock import patch

from config_layer import Config, reset_config


@pytest.fixture(autouse=True)
def clean_config():
    reset_config()
    yield
    reset_config()


class TestProdRequiredKeys:
    """prod profile 必填环境键硬失败"""

    def test_prod_missing_key_raises(self, tmp_path):
        """缺少任一必填 env 键 → load() raise"""
        cfg = Config(profile="prod")
        cfg._config_dir = str(tmp_path)
        # 模拟缺少 FUYAO_API_KEY
        env = {
            "FEISHU_APP_ID": "x",
            "FEISHU_APP_SECRET": "x",
            "SENSENOVA_KEY1": "x",
            "SENSENOVA_KEY2": "x",
            "SENSENOVA_KEY3": "x",
            "SENSENOVA_KEY4": "x",
            # FUYAO_API_KEY 缺失
        }
        with patch.dict(os.environ, env, clear=False):
            # 确保 FUYAO_API_KEY 不存在
            os.environ.pop("FUYAO_API_KEY", None)
            with pytest.raises(RuntimeError, match="FUYAO_API_KEY"):
                cfg.load()

    def test_prod_all_keys_present_passes(self, tmp_path):
        """所有必填键齐全 → load() 正常"""
        cfg = Config(profile="prod")
        cfg._config_dir = str(tmp_path)
        (tmp_path / "settings.yaml").write_text("data:\n  db_path: /tmp/test.db\n")
        env = {
            "FUYAO_API_KEY": "k",
            "FEISHU_APP_ID": "k",
            "FEISHU_APP_SECRET": "k",
            "SENSENOVA_KEY1": "k",
            "SENSENOVA_KEY2": "k",
            "SENSENOVA_KEY3": "k",
            "SENSENOVA_KEY4": "k",
            "TUSHARE_TOKEN": "k",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg.load()  # should not raise
            assert cfg._loaded is True

    def test_prod_tushare_missing_only_warns(self, tmp_path):
        """TUSHARE_TOKEN 缺失 → warning 级，不阻断"""
        cfg = Config(profile="prod")
        cfg._config_dir = str(tmp_path)
        (tmp_path / "settings.yaml").write_text("data:\n  db_path: /tmp/test.db\n")
        env = {
            "FUYAO_API_KEY": "k",
            "FEISHU_APP_ID": "k",
            "FEISHU_APP_SECRET": "k",
            "SENSENOVA_KEY1": "k",
            "SENSENOVA_KEY2": "k",
            "SENSENOVA_KEY3": "k",
            "SENSENOVA_KEY4": "k",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("TUSHARE_TOKEN", None)
            cfg.load()  # should not raise
            assert cfg._loaded is True

    def test_prod_missing_db_path_raises(self, tmp_path):
        """prod 缺 data.db_path → raise"""
        cfg = Config(profile="prod")
        cfg._config_dir = str(tmp_path)
        env = {
            "FUYAO_API_KEY": "k",
            "FEISHU_APP_ID": "k",
            "FEISHU_APP_SECRET": "k",
            "SENSENOVA_KEY1": "k",
            "SENSENOVA_KEY2": "k",
            "SENSENOVA_KEY3": "k",
            "SENSENOVA_KEY4": "k",
        }
        with patch.dict(os.environ, env, clear=False):
            # tmp_path 无 YAML → data.db_path 不存在
            with pytest.raises(RuntimeError, match="data.db_path"):
                cfg.load()


class TestDevRequiredKeys:
    """dev profile 仅 data.db_path 必填"""

    def test_dev_missing_db_path_raises(self, tmp_path):
        cfg = Config(profile="dev")
        cfg._config_dir = str(tmp_path)
        with pytest.raises(RuntimeError, match="data.db_path"):
            cfg.load()

    def test_dev_with_db_path_passes(self, tmp_path):
        cfg = Config(profile="dev")
        cfg._config_dir = str(tmp_path)
        # 手动写入最小 YAML
        (tmp_path / "settings.yaml").write_text("data:\n  db_path: /tmp/test.db\n")
        cfg.load()
        assert cfg._loaded is True

    def test_dev_no_env_keys_needed(self, tmp_path, monkeypatch):
        """dev 不需要任何 env 秘密键"""
        cfg = Config(profile="dev")
        cfg._config_dir = str(tmp_path)
        (tmp_path / "settings.yaml").write_text("data:\n  db_path: /tmp/test.db\n")
        # 清除所有秘密 env (CF-02: monkeypatch.delenv 自动恢复, 原裸 os.environ.pop
        # 不恢复 → 秘密键泄漏删除, 与 CONFIG_ENV=prod 泄漏合谋致全量 25F+43E)
        for k in ["FUYAO_API_KEY", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
                  "SENSENOVA_KEY1", "SENSENOVA_KEY2", "SENSENOVA_KEY3", "SENSENOVA_KEY4"]:
            monkeypatch.delenv(k, raising=False)
        cfg.load()
        assert cfg._loaded is True


class TestSecretRegistration:
    """秘密注册机制"""

    def test_secrets_registered_after_load(self, tmp_path):
        cfg = Config(profile="dev")
        cfg._config_dir = str(tmp_path)
        (tmp_path / "settings.yaml").write_text("data:\n  db_path: /tmp/test.db\n")
        cfg.load()
        # secrets 键应存在于 registry
        assert "secrets.fuyao_api_key" in cfg._registry._entries
        entry = cfg._registry._entries["secrets.fuyao_api_key"]
        assert entry.secret is True
        assert entry.env_var == "FUYAO_API_KEY"

    def test_secret_value_from_env(self, tmp_path):
        cfg = Config(profile="dev")
        cfg._config_dir = str(tmp_path)
        (tmp_path / "settings.yaml").write_text("data:\n  db_path: /tmp/test.db\n")
        with patch.dict(os.environ, {"FUYAO_API_KEY": "test_secret_123"}):
            cfg.load()
            assert cfg.get("secrets.fuyao_api_key") == "test_secret_123"

    def test_secret_masked_in_to_dict(self, tmp_path):
        cfg = Config(profile="dev")
        cfg._config_dir = str(tmp_path)
        (tmp_path / "settings.yaml").write_text("data:\n  db_path: /tmp/test.db\n")
        with patch.dict(os.environ, {"FUYAO_API_KEY": "real_value"}):
            cfg.load()
            d = cfg.to_dict(show_secrets=False)
            assert d["secrets.fuyao_api_key"] == "******"


class TestHealthSecretsCheck:
    """health() 新增 required_secrets 检查项"""

    def test_health_has_secrets_check(self):
        cfg = Config(profile="test")
        result = cfg.health()
        check_names = [c.name for c in result.checks]
        assert "required_secrets" in check_names

    def test_health_secrets_pass_non_prod(self):
        """非 prod profile 缺秘密不 fail"""
        cfg = Config(profile="dev")
        cfg._loaded = True
        cfg.set("data.db_path", "/tmp/test.db")
        result = cfg.health()
        secrets_check = next(c for c in result.checks if c.name == "required_secrets")
        assert secrets_check.status == "pass"

    def test_health_secrets_fail_prod_missing(self, monkeypatch):
        """prod profile 缺秘密 → fail"""
        cfg = Config(profile="prod")
        cfg._loaded = True
        cfg.set("data.db_path", "/tmp/test.db")
        # CF-02: monkeypatch.delenv 自动恢复 (原裸 pop 泄漏, 校验强度不变)
        for k in ["FUYAO_API_KEY", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
                  "SENSENOVA_KEY1", "SENSENOVA_KEY2", "SENSENOVA_KEY3", "SENSENOVA_KEY4"]:
            monkeypatch.delenv(k, raising=False)
        result = cfg.health()
        secrets_check = next(c for c in result.checks if c.name == "required_secrets")
        assert secrets_check.status == "fail"

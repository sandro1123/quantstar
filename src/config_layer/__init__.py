"""Config Layer — 统一配置入口"""

import importlib
import logging
import os
from collections.abc import Callable
from typing import Any

from common.decorators import catch_quantstar_error, with_timeout
from config_layer.loader import ConfigLoader
from config_layer.registry import ConfigRegistry
from config_layer.validator import ConfigValidator

logger = logging.getLogger("config_layer")

# ---------------------------------------------------------------------------
# CF-01: 必填环境键清单 (D-27 拍板; 卡G1 2026-09-20 用户裁定解耦)
# 必填 = 3 真核心; LLM 供应商密钥 (SENSENOVA/TUSHARE 等) 降 warning 级 —
# sensenova 已是深层备链 (llm.yaml:66), 开源仓用户不一定用它, 不能锁死;
# 未填的腿自动降级走备链 (llm.yaml 支持多供应商, 按启用者填)。
# ---------------------------------------------------------------------------
_PROD_REQUIRED_ENV_KEYS = [
    "FUYAO_API_KEY",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
]
_PROD_WARNING_ENV_KEYS = [
    "TUSHARE_TOKEN",
    "SENSENOVA_KEY1",
    "SENSENOVA_KEY2",
    "SENSENOVA_KEY3",
    "SENSENOVA_KEY4",
]

# 秘密注册表：key -> (env_var, description)
_SECRET_REGISTRY: dict[str, tuple[str, str]] = {
    "secrets.fuyao_api_key": ("FUYAO_API_KEY", "扶摇金融数据 API 密钥"),
    "secrets.feishu_app_id": ("FEISHU_APP_ID", "飞书应用 ID"),
    "secrets.feishu_app_secret": ("FEISHU_APP_SECRET", "飞书应用密钥"),
    # 卡G1 (2026-09-20): sensenova 已降深层备链 (llm.yaml:66), 注册保留
    # (降级≠删除), 仅必填级解除 — 缺失走 warning 不拒载。
    "secrets.sensenova_key1": ("SENSENOVA_KEY1", "商汤大模型密钥 1 (深层备链)"),
    "secrets.sensenova_key2": ("SENSENOVA_KEY2", "商汤大模型密钥 2 (深层备链)"),
    "secrets.sensenova_key3": ("SENSENOVA_KEY3", "商汤大模型密钥 3 (深层备链)"),
    "secrets.sensenova_key4": ("SENSENOVA_KEY4", "商汤大模型密钥 4 (深层备链)"),
    "secrets.tushare_token": ("TUSHARE_TOKEN", "Tushare 数据令牌"),
}


class Config:
    def __init__(self, profile: str | None = None):
        if profile:
            self._profile = profile
            self._profile_source = "explicit"
        elif os.environ.get("CONFIG_ENV"):
            self._profile = os.environ["CONFIG_ENV"]
            self._profile_source = "env:CONFIG_ENV"
        else:
            self._profile = "dev"
            self._profile_source = "default"
        self._registry = ConfigRegistry()
        self._loader = ConfigLoader()
        self._validator = ConfigValidator()
        self._config_dir = os.environ.get(
            "CONFIG_DIR",
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config"),
        )
        self._loaded = False

    # ------------------------------------------------------------------
    # CF-01: 秘密注册 + 必填校验
    # ------------------------------------------------------------------
    def _register_secrets(self):
        """将秘密键以 env_var/secret 语义登记进 registry（激活 env_var/secret/脱敏三机制）"""
        for key, (env_var, desc) in _SECRET_REGISTRY.items():
            self._registry.register(
                key=key,
                type_=str,
                default=None,
                description=desc,
                secret=True,
                env_var=env_var,
            )

    def _check_required(self):
        """启动必填校验（CF-01 / D-27）。

        prod: 3 真核心 env 键 + data.db_path 硬失败 (卡G1 2026-09-20:
        SENSENOVA 降 warning — 备链不锁死); 其余 LLM/数据源键 warning。
        dev:  仅 data.db_path 硬失败。
        其他 profile: 不硬失败（宽松模式）。
        """
        profile = self._profile

        if profile == "prod":
            missing_hard = [k for k in _PROD_REQUIRED_ENV_KEYS if not os.environ.get(k)]
            if not self.get("data.db_path"):
                missing_hard.append("data.db_path (配置键)")
            if missing_hard:
                raise RuntimeError(
                    f"[CF-01] prod 启动校验失败 — 缺少必填项: {missing_hard}\n"
                    f"修复指引: 环境变量请在 .env 或 crontab 中设置; "
                    f"data.db_path 请在 config/settings.yaml 或 profiles/prod.yaml 中配置。"
                )
            # warning 级
            for k in _PROD_WARNING_ENV_KEYS:
                if not os.environ.get(k):
                    logger.warning(f"[CF-01] prod 环境缺少 {k}（warning 级，不阻断启动）")

        elif profile == "dev":
            if not self.get("data.db_path"):
                raise RuntimeError(
                    "[CF-01] dev 启动校验失败 — 缺少必填项: ['data.db_path']\n"
                    "修复指引: 请在 config/settings.yaml 中配置 data.db_path。"
                )
        # 其他 profile: 宽松模式，不做硬失败

    # ------------------------------------------------------------------
    # CF-04: get/set 等纯内存方法摘除装饰器
    # ------------------------------------------------------------------
    @catch_quantstar_error(layer_name="config")
    @with_timeout(timeout_s=5)
    def register(
        self,
        key: str,
        type_: type,
        default: Any = None,
        description: str = "",
        secret: bool = False,
        env_var: str | None = None,
        constraints: str | None = None,
    ):
        self._registry.register(key, type_, default, description, secret, env_var, constraints)

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值。CF-04: 纯内存操作，无装饰器。

        异常语义：内部任何异常 → 返回调用方 default + warning（不再吞成 None）。
        """
        try:
            return self._registry.get(key, default)
        except Exception as e:
            logger.warning(f"[CF-04] config.get({key!r}) 内部异常，返回 default: {e}")
            return default

    def get_section(self, prefix: str) -> dict:
        """获取以 prefix 开头的所有键，重建嵌套 dict 结构。CF-04: 无装饰器。"""
        result = {}
        for key, entry in self._registry._entries.items():
            if key.startswith(prefix + ".") or key == prefix:
                rel = key[len(prefix):].lstrip(".")
                if not rel:
                    continue
                parts = rel.split(".")
                d = result
                for p in parts[:-1]:
                    d = d.setdefault(p, {})
                d[parts[-1]] = entry.value
        return result

    def set(self, key: str, value: Any):
        """设置配置值。CF-04: 纯内存操作，无装饰器。"""
        self._registry.set(key, value)

    def get_profile(self) -> str:
        return self._profile

    def group_keys(self, prefix: str) -> list[str]:
        """返回以 prefix 开头的所有键（去掉 prefix 前缀）"""
        keys = []
        for key in self._registry._entries:
            if key.startswith(prefix + "."):
                keys.append(key[len(prefix) + 1:])
            elif key == prefix:
                keys.append("")
        return keys

    def group_dict(self, prefix: str) -> dict[str, Any]:
        """返回以 prefix 开头的所有键值对，扁平化为 {relative_key: value}"""
        result = {}
        for key, entry in self._registry._entries.items():
            if key.startswith(prefix + "."):
                rel = key[len(prefix) + 1:]
                result[rel] = entry.value
            elif key == prefix:
                result[""] = entry.value
        return result

    @catch_quantstar_error(layer_name="config")
    @with_timeout(timeout_s=15)
    def instantiate(self, prefix: str, **extra_kwargs) -> Any:
        """根据 _target_ 配置项动态实例化类。

        读取 prefix._target_ 获取类路径 (如 "module.submodule.ClassName")，
        自动 import 并用 prefix 下其余键值对作为 kwargs 实例化。
        extra_kwargs 会覆盖配置中的同名参数。
        """
        target = self.get(f"{prefix}._target_")
        if not target:
            raise ValueError(f"配置 '{prefix}' 缺少 _target_ 字段")
        module_path, _, class_name = target.rpartition(".")
        if not module_path:
            raise ValueError(f"_target_ 格式错误: {target}，应为 'module.ClassName'")
        try:
            mod = importlib.import_module(module_path)
        except ImportError as e:
            raise ImportError(f"无法导入 {module_path}: {e}") from e
        cls = getattr(mod, class_name, None)
        if cls is None:
            raise AttributeError(f"{module_path} 中找不到 {class_name}")
        kwargs = self.group_dict(prefix)
        kwargs.pop("", None)
        kwargs.pop("_target_", None)
        kwargs.update(extra_kwargs)
        return cls(**kwargs)

    @with_timeout(timeout_s=10)
    def load(self):
        """加载配置 + 必填校验。

        CF-01: 摘除 @catch_quantstar_error——必填校验失败必须 raise 传播到
        bootstrap 实现启动硬失败（该装饰器会吞掉异常返回 None）。
        保留 @with_timeout：文件 I/O 超时保护，且内部异常经 future.result()
        原样重抛，不影响硬失败语义。
        """
        self._loader.load_profile(self._registry, self._profile, self._config_dir)
        result = self._validator.validate(self._registry)
        if result.errors:
            raise RuntimeError(f"配置校验失败: {result.errors}")
        # CF-01: 秘密注册 + 必填校验
        self._register_secrets()
        self._check_required()
        self._loaded = True

    def to_dict(self, show_secrets: bool = False) -> dict:
        return self._registry.to_dict(show_secrets)

    def on_change(self, key: str, callback: Callable):
        """注册配置变更回调（registry 级，set 触发）。CF-02: 保留。"""
        self._registry.on_change(key, callback)

    def health(self):
        from ops_layer.core.models import HealthCheck, LayerHealth
        checks = []

        # 1. Config loaded
        checks.append(HealthCheck(
            name="config_loaded",
            status="pass" if self._loaded else "warn",
            detail=f"Profile: {self._profile} (source: {self._profile_source})"))

        # 2. Required keys check
        hard_required = ["data.db_path"]
        soft_required = ["data.sources", "strategy.factors"]

        hard_found = [k for k in hard_required if self.get(k) is not None]
        if len(hard_found) < len(hard_required):
            key_status = "fail"
            detail = f"缺少硬必需配置: {set(hard_required) - set(hard_found)}"
        else:
            soft_found = [k for k in soft_required if self.get(k) is not None]
            key_status = "pass"
            detail = f"硬必需 {len(hard_found)}/{len(hard_required)}, 软必需 {len(soft_found)}/{len(soft_required)}(fallback)"

        checks.append(HealthCheck(
            name="required_keys",
            status=key_status,
            detail=detail))

        # 3. CF-01: Required secrets check
        secret_status = "pass"
        secret_detail_parts = []
        for key, (env_var, _desc) in _SECRET_REGISTRY.items():
            present = bool(os.environ.get(env_var))
            if not present and self._profile == "prod":
                secret_status = "fail"
                secret_detail_parts.append(f"{env_var}=缺失")
            elif not present:
                secret_detail_parts.append(f"{env_var}=缺失(非prod)")
        if secret_detail_parts:
            secret_detail = f"秘密检查: {len(secret_detail_parts)} 项缺失 ({', '.join(secret_detail_parts[:3])}{'...' if len(secret_detail_parts) > 3 else ''})"
        else:
            secret_detail = f"秘密检查: {len(_SECRET_REGISTRY)}/{len(_SECRET_REGISTRY)} 项就绪"
        checks.append(HealthCheck(
            name="required_secrets",
            status=secret_status,
            detail=secret_detail))

        # Overall
        status = "green"
        for c in checks:
            if c.status == "fail":
                status = "red"
                break
            elif c.status == "warn":
                status = "yellow"
        return LayerHealth(
            layer="config",
            status=status,
            checks=checks,
            summary=f"Config Layer {'正常' if status == 'green' else '需关注'} ({self._profile})")

    def shutdown(self, timeout: float = 5.0):
        """CF-02: 热加载已删除（一次性进程架构，配置变更需重启进程）。保留接口兼容。"""
        pass


_config_instance: Config | None = None


def get_config(profile: str | None = None) -> Config:
    global _config_instance
    if _config_instance is None:
        _config_instance = Config(profile)
    return _config_instance


def reset_config():
    global _config_instance
    _config_instance = None


def cfg_get(key: str, default=None):
    """Convenience function to get a config value without creating a Config instance."""
    config = get_config()
    if not config._loaded:
        config.load()
    return config.get(key, default)

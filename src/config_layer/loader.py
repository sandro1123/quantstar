"""YAML 加载 + 多层合并 + 变量插值"""

import os
import re

import yaml

from common.decorators import catch_quantstar_error
from config_layer.registry import ConfigRegistry


class ConfigLoader:
    def __init__(self):
        self._interp_pattern = re.compile(r'\$\{([^}]+)\}')

    @catch_quantstar_error(layer_name="config")
    def load_file(self, registry: ConfigRegistry, file_path: str):
        if not os.path.exists(file_path):
            return
        with open(file_path, "r") as f:
            data = yaml.safe_load(f)
        if data is None:
            return
        self._apply(registry, data, "")

    @catch_quantstar_error(layer_name="config")
    def load_profile(self, registry: ConfigRegistry, profile: str, config_dir: str):
        defaults_dir = os.path.join(config_dir, "defaults")
        profiles_dir = os.path.join(config_dir, "profiles")
        settings_path = os.path.join(config_dir, "settings.yaml")
        local_path = os.path.join(config_dir, "local.yaml")

        if os.path.isdir(defaults_dir):
            for f in sorted(os.listdir(defaults_dir)):
                if f.endswith(".yaml"):
                    self.load_file(registry, os.path.join(defaults_dir, f))
        self.load_file(registry, settings_path)
        self.load_file(registry, os.path.join(profiles_dir, f"{profile}.yaml"))
        self.load_file(registry, local_path)

        self._interpolate(registry)

    def _apply(self, registry: ConfigRegistry, data: dict, prefix: str):
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                self._apply(registry, value, full_key)
            else:
                registry.set(full_key, value)

    def _interpolate(self, registry: ConfigRegistry):
        import logging
        _logger = logging.getLogger("config_layer.loader")
        for key in list(registry._entries.keys()):
            entry = registry._entries[key]
            val = entry.value
            if isinstance(val, str) and self._interp_pattern.search(val):
                def _resolve(m):
                    ref = m.group(1)
                    resolved = registry.get(ref, "")
                    if resolved == "":
                        _logger.warning(f"[CF-07] 插值引用 ${{{ref}}} 解析为空（键不存在或值为空）")
                    return str(resolved)
                resolved = self._interp_pattern.sub(_resolve, val)
                entry.value = resolved

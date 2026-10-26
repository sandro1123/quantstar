import os

from delivery_layer.message import Message


class Router:
    """投递路由 v2 (第四波): 白名单闸 + 配置单源。

    - 配置单源铁律: 只读 config registry (delivery.push_rules),
      push_rules.yaml 旧文件直读后门已删 (双轨是 09-10 病理单第三条)。
    - 白名单闸 (宪法 #12): 未注册 content_type 在 production 默认拒;
      debug 模式保留 dm 兜底 (开发可见性)。
    """

    def __init__(self, config_dir: str | None = None):
        # config_dir 保留参数兼容旧调用方; 路由不再读文件
        self._config_dir = config_dir or os.environ.get(
            "CONFIG_DIR",
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config"),
        )

    def resolve(self, message: Message, report_mode: str = "debug") -> list[str]:
        if message.loudness == "off":
            return []
        if message.force_channel:
            return [message.force_channel]
        if message.severity == "critical" or message.force_dm:
            return ["feishu_dm"]

        rules = self._load_rules()
        return self._resolve_from_rules(rules, message, report_mode)

    def _resolve_from_rules(self, rules: dict, message: Message,
                            report_mode: str) -> list[str]:
        """白名单闸: 规则缺 type 或缺 mode → 空表 (production 默认拒)。

        debug 模式保留 dm 兜底 (开发可见性 — 白名单闸语义只在
        production 收紧, 开发面不能静默失联)。
        """
        content_rules = rules.get(message.content_type, {})
        channels = content_rules.get(report_mode, [])
        if channels:
            return list(channels)
        if report_mode == "debug":
            return ["feishu_dm"]
        return []

    def _load_rules(self) -> dict:
        """配置单源: 只读 config registry; registry 无数据=显式空 dict。

        push_rules.yaml 文件直读后门已删 (第四波病理单销案: 双轨后门
        让 yaml 文件与 registry 两处真相可分叉)。
        """
        try:
            from config_layer import get_config
            config = get_config()
            if config._loaded:
                rules = config.get_section("delivery.push_rules")
                if rules:
                    return rules
        except Exception:
            pass
        return {}

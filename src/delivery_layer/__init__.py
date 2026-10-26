# ruff: noqa: F401
from __future__ import annotations

from common.decorators import catch_quantstar_error, with_timeout
from common.logging import get_logger
from config_layer import Config
from delivery_layer.channels import (
    FeishuDMChannel,
    FeishuGroupChannel,
    FeishuHitlDMChannel,
    OpsArchiveChannel,
)
from delivery_layer.channels.registry import ChannelRegistry
from delivery_layer.executor import DeliveryExecutor
from delivery_layer.message import Message
from delivery_layer.router import Router

logger = get_logger("delivery")


class DeliveryLayer:
    def __init__(self, config: Config | None = None, db=None):
        self.config = config
        self.registry = ChannelRegistry()
        self.router = Router()
        # F5.2c: db 传入时启用 delivery_log 持久化
        self.executor = DeliveryExecutor(self.registry, self.router, db=db)
        self._register_channels()

    def _register_channels(self):
        self.registry.register(FeishuGroupChannel(config=self.config))
        self.registry.register(FeishuDMChannel(config=self.config))
        # Phase 2: HITL 专用通道 (新 app 凭据 + union_id), 审批/通知卡走此通道
        self.registry.register(FeishuHitlDMChannel(config=self.config))
        # 第四波 (2026-09-11): 运维归档通道注册 (宪法#12 运维件分离)
        self.registry.register(OpsArchiveChannel())

    @catch_quantstar_error(layer_name="delivery")
    def send(self, message: Message, report_mode: str = "debug"):
        return self.executor.send(message, report_mode)

    @catch_quantstar_error(layer_name="delivery")
    def send_async(self, message: Message, report_mode: str = "debug"):
        return self.executor.send_async(message, report_mode)

    @catch_quantstar_error(layer_name="delivery")
    def update(self, remote_message_id: str, content: dict,
               channel_name: str = "feishu_dm"):
        """R3/D3: 原地更新已发卡片 (触价卡 PATCH 语义, 范式 4)。"""
        return self.executor.update(remote_message_id, content, channel_name)

    def health(self):
        from ops_layer.core.models import HealthCheck, LayerHealth
        checks = []

        # 1. Channel count
        try:
            total = self.registry.count()
            checks.append(HealthCheck(name="channels_count", status="pass", detail=f"已注册 {total} 个渠道"))
        except Exception as e:
            checks.append(HealthCheck(name="channels_count", status="warn", detail=f"渠道计数失败: {e}"))
            total = 0

        # 2. Per-channel health probe (call health() on each)
        healthy = 0
        try:
            for channel in self.registry.get_all():
                name = getattr(channel, "name", "unknown")
                try:
                    ch_health = channel.health()
                    if isinstance(ch_health, dict):
                        ok = ch_health.get("status") in ("pass", True)
                        detail = ch_health.get("detail", "正常")
                    else:
                        ok = bool(ch_health)
                        detail = "正常" if ok else "健康检查失败"
                    if ok:
                        healthy += 1
                        checks.append(HealthCheck(name=f"channel_{name}", status="pass", detail=detail))
                    else:
                        checks.append(HealthCheck(name=f"channel_{name}", status="warn", detail=detail))
                except AttributeError:
                    healthy += 1
                    checks.append(HealthCheck(name=f"channel_{name}", status="pass", detail="已注册"))
                except Exception as e:
                    checks.append(HealthCheck(name=f"channel_{name}", status="fail", detail=f"探针异常: {e}"))
        except Exception as e:
            checks.append(HealthCheck(name="channel_probe", status="warn", detail=f"渠道健康检查失败: {e}"))

        # 3. Executor health
        try:
            executor_ok = self.executor is not None
            checks.append(HealthCheck(name="executor", status="pass" if executor_ok else "fail",
                detail="发送执行器就绪" if executor_ok else "发送执行器未初始化"))
        except Exception as e:
            checks.append(HealthCheck(name="executor", status="fail", detail=f"执行器异常: {e}"))

        # Overall
        status = "green"
        for c in checks:
            if c.status == "fail":
                status = "red"
                break
            elif c.status == "warn":
                status = "yellow"
        return LayerHealth(layer="delivery", status=status, checks=checks,
            summary=f"Delivery Layer {'正常' if status == 'green' else '需关注'} ({healthy}/{total} 渠道健康)")

    def shutdown(self, timeout: float = 5.0):
        self.executor._wait_pending(timeout)
        return True

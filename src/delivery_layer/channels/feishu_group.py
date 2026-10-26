"""飞书群通道 — F5.2a: App API 模式 (复用 dm 的 token+push_via_api)

优先 API 模式 (FEISHU_APP_ID/FEISHU_APP_SECRET + FEISHU_GROUP_ID 群 chat_id),
API 失败降级 webhook (现状逻辑保留)。
F5.2b: health() 真探测 — API 模式探测 token 可获取 + 群可达, webhook 模式探测 env 存在。
"""

import os
import time

import requests

from common.logging import get_logger
from config_layer import Config
from delivery_layer.channels.base import Channel, DeliveryResult
from delivery_layer.channels.feishu_dm import (
    FEISHU_API_BASE,
    _get_cached_token,
    _push_via_api,
)
from delivery_layer.message import Message

logger = get_logger("delivery.feishu_group")


class FeishuGroupChannel(Channel):
    def __init__(self, config: Config | None = None):
        self.config = config

    def _env(self, key: str, default: str = "") -> str:
        if self.config:
            val = self.config.get(f"delivery.feishu.{key.lower()}")
            if val:
                return str(val)
        return os.getenv(key, default)

    @property
    def name(self) -> str:
        return "feishu_group"

    def _api_creds(self) -> tuple[str, str, str]:
        return (self._env("FEISHU_APP_ID"), self._env("FEISHU_APP_SECRET"),
                self._env("FEISHU_GROUP_ID"))

    def send(self, message: Message) -> DeliveryResult:
        start = time.time()
        app_id, app_secret, group_chat_id = self._api_creds()

        # F5.2a: 优先 App API 模式 (群 chat_id)
        if app_id and app_secret and group_chat_id:
            token = _get_cached_token(app_id, app_secret)
            if token:
                ok, error_msg = _push_via_api(group_chat_id, message.content, token)
                if ok:
                    return DeliveryResult(
                        message_id=message.message_id, channel=self.name,
                        status="success", duration_ms=int((time.time() - start) * 1000),
                    )
                logger.debug(f"群 API 推送失败, 尝试 webhook 降级: {error_msg}")

        # Webhook 降级 (现状逻辑保留)
        webhook_url = self._env("FEISHU_GROUP_WEBHOOK") or self._env("FEISHU_WEBHOOK_URL")
        if not webhook_url:
            return DeliveryResult(
                message_id=message.message_id, channel=self.name,
                status="failed",
                error_msg="无可用凭据: 需要 FEISHU_APP_ID+FEISHU_APP_SECRET+FEISHU_GROUP_ID 或 FEISHU_GROUP_WEBHOOK",
                duration_ms=int((time.time() - start) * 1000),
            )
        try:
            resp = requests.post(webhook_url, json=message.content, timeout=10)
            resp.raise_for_status()
            body = resp.json() if resp.content else {}
            code = body.get("code")
            if code not in (0, None):
                return DeliveryResult(
                    message_id=message.message_id, channel=self.name,
                    status="failed",
                    error_msg=f"feishu webhook code={code}: {body.get('msg', '')}".strip(": "),
                    duration_ms=int((time.time() - start) * 1000),
                )
            return DeliveryResult(
                message_id=message.message_id, channel=self.name,
                status="success", duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return DeliveryResult(
                message_id=message.message_id, channel=self.name,
                status="failed", error_msg=str(e),
                duration_ms=int((time.time() - start) * 1000),
            )

    def health(self) -> bool:
        """F5.2b: 真探测 — API 模式探测 token 可获取 + 群可达; webhook 模式探测 env 存在"""
        app_id, app_secret, group_chat_id = self._api_creds()
        if app_id and app_secret and group_chat_id:
            token = _get_cached_token(app_id, app_secret)
            if not token:
                return False
            # 群可达探测: GET 群信息 (不发消息)
            try:
                resp = requests.get(
                    f"{FEISHU_API_BASE}/im/v1/chats/{group_chat_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                data = resp.json()
                if data.get("code") == 0:
                    return True
                logger.warning(f"群健康探测失败: code={data.get('code')} {data.get('msg', '')}")
                return False
            except Exception as e:
                logger.warning(f"群健康探测异常: {e}")
                return False
        # webhook 模式: env 存在即视为可用 (webhook 无法无副作用探测)
        return bool(self._env("FEISHU_GROUP_WEBHOOK") or self._env("FEISHU_WEBHOOK_URL"))

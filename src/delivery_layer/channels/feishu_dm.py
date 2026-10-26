"""飞书私聊通道 — API 模式 (tenant_access_token + chat_id)

优先使用 API 模式（APP_ID/APP_SECRET + FEISHU_DM_ID），
fallback 到 webhook 模式（FEISHU_DM_WEBHOOK）。
"""

import json
import os
import threading
import time

import requests

from common.logging import get_logger
from config_layer import Config
from delivery_layer.channels.base import Channel, DeliveryResult
from delivery_layer.message import Message

logger = get_logger("delivery.feishu_dm")

# Phase 2: 缓存按 app_id 键控 — 新旧 app 共存时互不覆盖 token。
# (D 稿 §0.2 原称"已按 (app_id, app_secret) 键控"系事实错误, 原为单槽缓存, 此处修正。)
_token_cache: dict = {}
_token_lock = threading.Lock()

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


def _get_cached_token(app_id: str, app_secret: str) -> str:
    now = time.time()
    with _token_lock:
        entry = _token_cache.get(app_id)
        if entry and entry["expires_at"] > now + 60:
            return entry["token"]
    try:
        resp = requests.post(
            f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 0:
            token = data["tenant_access_token"]
            with _token_lock:
                _token_cache[app_id] = {
                    "token": token,
                    "expires_at": now + data.get("expire", 7200),
                }
            return token
        logger.error(f"飞书 token 获取失败: {data}")
    except Exception as e:
        logger.error(f"飞书 token 请求异常: {e}")
    return ""


def _push_via_api_ex(chat_id: str, content: dict, token: str) -> tuple[bool, str, str]:
    """返回 (是否成功, 错误信息, 远端 message_id)。

    HITL 审批卡需要远端 message_id 才能 PATCH 原卡片。既有调用方
    (feishu_group / alert_manager / test_s5_delivery) 按 2 元组解包,
    故此处另开 _ex 变体, _push_via_api 降级为兼容包装。
    """
    msg_type = content.get("msg_type", "text")
    if msg_type == "interactive":
        body_content = json.dumps(content.get("card", content), ensure_ascii=False)
    elif msg_type == "text":
        body_content = json.dumps(content.get("content", content), ensure_ascii=False)
    else:
        body_content = json.dumps(content, ensure_ascii=False)

    payload = {
        "receive_id": chat_id,
        "msg_type": msg_type,
        "content": body_content,
    }
    try:
        resp = requests.post(
            f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type=chat_id",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        data = resp.json()
        if data.get("code") == 0:
            return True, "", (data.get("data") or {}).get("message_id", "")
        error_msg = data.get("msg", f"code={data.get('code')}")
        logger.warning(f"飞书 API 推送失败: {error_msg}")
        return False, error_msg, ""
    except Exception as e:
        logger.error(f"飞书 API 推送异常: {e}")
        return False, str(e), ""


def _push_via_api(chat_id: str, content: dict, token: str) -> tuple[bool, str]:
    """兼容包装 — 保持既有 2 元组契约不变"""
    ok, error_msg, _ = _push_via_api_ex(chat_id, content, token)
    return ok, error_msg

def _patch_via_api(message_id: str, content: dict, token: str) -> tuple[bool, str]:
    """R3/D3: 原地更新已发卡片 (PUT /im/v1/messages/{message_id})。

    触价卡 PATCH 语义: 同一张卡「进入区间」→「止盈/止损」原地更新,
    不重发新卡 (范式 4)。HITL Phase1 的 card_message_id 落库是先例,
    此处补上真正的更新调用。
    """
    msg_type = content.get("msg_type", "interactive")
    if msg_type == "interactive":
        body_content = json.dumps(content.get("card", content), ensure_ascii=False)
    else:
        body_content = json.dumps(content.get("content", content), ensure_ascii=False)
    payload = {"msg_type": msg_type, "content": body_content}
    try:
        resp = requests.put(
            f"{FEISHU_API_BASE}/im/v1/messages/{message_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        data = resp.json()
        if data.get("code") == 0:
            return True, ""
        error_msg = data.get("msg", f"code={data.get('code')}")
        logger.warning(f"飞书卡片更新失败: {error_msg}")
        return False, error_msg
    except Exception as e:
        logger.error(f"飞书卡片更新异常: {e}")
        return False, str(e)


def _push_via_webhook(webhook_url: str, content: dict) -> tuple[bool, str]:
    try:
        resp = requests.post(webhook_url, json=content, timeout=10)
        resp.raise_for_status()
        return True, ""
    except Exception as e:
        return False, str(e)


class FeishuDMChannel(Channel):
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
        return "feishu_dm"

    def send(self, message: Message) -> DeliveryResult:
        start = time.time()
        app_id = self._env("FEISHU_APP_ID")
        app_secret = self._env("FEISHU_APP_SECRET")
        dm_chat_id = self._env("FEISHU_DM_ID")

        # 优先 API 模式
        if app_id and app_secret and dm_chat_id:
            token = _get_cached_token(app_id, app_secret)
            if token:
                ok, error_msg, remote_id = _push_via_api_ex(dm_chat_id, message.content, token)
                if ok:
                    return DeliveryResult(
                        message_id=message.message_id, channel=self.name,
                        status="success", duration_ms=int((time.time() - start) * 1000),
                        remote_message_id=remote_id,
                    )
                # API 失败，尝试 fallback webhook
                logger.debug(f"API 推送失败，尝试 webhook fallback: {error_msg}")

        # Fallback webhook 模式
        webhook_url = self._env("FEISHU_DM_WEBHOOK")
        if webhook_url:
            ok, error_msg = _push_via_webhook(webhook_url, message.content)
            return DeliveryResult(
                message_id=message.message_id, channel=self.name,
                status="success" if ok else "failed",
                error_msg=error_msg,
                duration_ms=int((time.time() - start) * 1000),
            )

        return DeliveryResult(
            message_id=message.message_id, channel=self.name,
            status="failed", error_msg="无可用凭据: 需要 FEISHU_APP_ID+FEISHU_APP_SECRET+FEISHU_DM_ID 或 FEISHU_DM_WEBHOOK",
            duration_ms=int((time.time() - start) * 1000),
        )

    def patch(self, message_id: str, content: dict) -> DeliveryResult:
        """R3/D3: 原地更新已发卡片 (触价卡 PATCH 语义, 范式 4)。

        仅 API 模式支持 (webhook 无 message_id 概念); 凭据缺失/更新失败
        返回 failed, 调用方据此降级重发新卡。
        """
        start = time.time()
        app_id = self._env("FEISHU_APP_ID")
        app_secret = self._env("FEISHU_APP_SECRET")
        if app_id and app_secret and message_id:
            token = _get_cached_token(app_id, app_secret)
            if token:
                ok, error_msg = _patch_via_api(message_id, content, token)
                return DeliveryResult(
                    message_id=message_id, channel=self.name,
                    status="success" if ok else "failed",
                    error_msg=error_msg,
                    duration_ms=int((time.time() - start) * 1000),
                )
        return DeliveryResult(
            message_id=message_id, channel=self.name,
            status="failed", error_msg="无可用凭据或缺 message_id, 无法原地更新",
            duration_ms=int((time.time() - start) * 1000),
        )

    def health(self) -> bool:
        """F5.2b: 真探测 — API 模式探测 token 可获取; 不可获取则查 webhook 降级可用性"""
        app_id = self._env("FEISHU_APP_ID")
        app_secret = self._env("FEISHU_APP_SECRET")
        dm_chat_id = self._env("FEISHU_DM_ID")
        if app_id and app_secret and dm_chat_id:
            if _get_cached_token(app_id, app_secret):
                return True
            logger.warning("DM 健康探测: token 获取失败, 检查 webhook 降级")
        return bool(self._env("FEISHU_DM_WEBHOOK"))

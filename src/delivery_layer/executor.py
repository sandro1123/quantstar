import hashlib
import json
import time
from concurrent.futures import Future, ThreadPoolExecutor

from common.decorators import catch_quantstar_error, with_timeout
from common.logging import get_logger
from common.retry import RetryPolicy, retry_with_backoff
from delivery_layer.channels.base import DeliveryResult
from delivery_layer.channels.registry import ChannelRegistry
from delivery_layer.message import Message
from delivery_layer.outbox import OutboxStore
from delivery_layer.router import Router

logger = get_logger("delivery.executor")


class RateLimiter:
    def __init__(self):
        self._cooldowns: dict[str, float] = {}
        self._dedup_cache: dict[str, float] = {}
        self._cooldown_map = {
            "intraday_alert": 1800,
            "evening_report": 1800,
            "morning_brief": 3600,
            "midday_brief": 3600,
            "weekly_summary": 86400,
            "ops_report": 3600,
            "dev_verify": 3600,
        }
        self._dedup_window = {
            "intraday_alert": 300,
            "evening_report": 3600,
            "morning_brief": 3600,
            "midday_brief": 3600,
            "weekly_summary": 86400,
            "alert_critical": 60,
        }

    def _content_hash(self, content: dict) -> str:
        try:
            raw = json.dumps(content, sort_keys=True, ensure_ascii=False)
            return hashlib.md5(raw.encode()).hexdigest()[:16]
        except Exception:
            return ""

    def can_send(self, content_type: str, dedup_key: str = "", content: dict | None = None) -> bool:
        if content_type == "alert_critical":
            return True

        # 冷却检查
        key = f"{content_type}:{dedup_key}"
        now = time.time()
        last = self._cooldowns.get(key, 0)
        cooldown = self._cooldown_map.get(content_type, 300)
        if now - last < cooldown:
            return False
        self._cooldowns[key] = now

        # 去重检查（相同内容在窗口内不重复发送）
        if content is not None:
            ch = self._content_hash(content)
            if ch:
                dup_key = f"dup:{content_type}:{ch}"
                last_dup = self._dedup_cache.get(dup_key, 0)
                dup_window = self._dedup_window.get(content_type, 300)
                if now - last_dup < dup_window:
                    return False
                self._dedup_cache[dup_key] = now

        return True


class DeliveryExecutor:
    # 缺省 fan_out 类型 (配置 delivery.fan_out_types 缺席时的回落)
    FAN_OUT_TYPES = frozenset({"intraday_alert", "alert_critical"})

    def __init__(self, registry: ChannelRegistry, router: Router, db=None):
        self.registry = registry
        self.router = router
        self._executor = ThreadPoolExecutor(max_workers=4)
        self.rate_limiter = RateLimiter()
        # F5.2c: 推送日志持久化 (db 为 None 时跳过, 不阻塞投递)
        self.log_store = None
        if db is not None:
            try:
                from delivery_layer.log_store import DeliveryLogStore
                self.log_store = DeliveryLogStore(db)
            except Exception:
                self.log_store = None
        # P0-2 ①: 链尾全败兜底落地仓 (纯文件系统, 零网络, 不阻塞投递)
        self.outbox = OutboxStore()

    def _is_fan_out(self, content_type: str) -> bool:
        """fan_out 判定收口到配置 delivery.fan_out_types (已加载即配置为准);
        配置缺席/异常回落内置默认 — 诚实降级, 绝不因配置病丢告警广播面。"""
        try:
            from config_layer import get_config
            cfg = get_config()
            if getattr(cfg, "_loaded", False):
                types = cfg.get("delivery.fan_out_types", None)
                if types is not None:
                    return content_type in set(types)
        except Exception:
            pass
        return content_type in self.FAN_OUT_TYPES

    def _log_result(self, result: DeliveryResult, content_type: str):
        if self.log_store is not None:
            self.log_store.record(result, content_type)

    def _log_sections(self, message: Message, date: str = ""):
        """B席 ⑤: 推送成功时落段留痕 (段清单+字符数+空段/降级标记)。

        与 _log_result 同点调用; 留痕失败不阻塞投递 (log_store 内部自吞)。
        仅对含 sections 的报告类消息生效 (message.content["sections"])。
        """
        if self.log_store is None:
            return
        try:
            # 判例#166: formatter 改形后 content["sections"] 丢失;
            #        先读 raw_content(就地保住的原形), 无则回退 content["sections"].
            content = getattr(message, "raw_content", None)
            if content is None or not isinstance(content, dict):
                content = getattr(message, "content", None)
            if not isinstance(content, dict) or not isinstance(content.get("sections"), list):
                return
            self.log_store.record_sections(message, date=date or str(content.get("date") or ""))
        except Exception as e:
            logger.warning(f"段留痕调用失败 (不影响投递): {e}")

    @catch_quantstar_error(layer_name="delivery")
    @with_timeout(timeout_s=30)
    def send(self, message: Message, report_mode: str = "debug") -> list[DeliveryResult]:
        if message.loudness == "off":
            return []
        if not self.rate_limiter.can_send(message.content_type, message.message_id, message.content):
            return []
        channels = self.router.resolve(message, report_mode)
        healthy = self.registry.get_healthy(channels)
        fan_out = self._is_fan_out(message.content_type)
        results = []
        for channel in healthy:
            try:
                policy = RetryPolicy(max_retries=3, backoff_base=5.0)
                result = retry_with_backoff(channel.send, policy, message)
                if result.status == "success":
                    self.registry.mark_healthy(channel.name)
                else:
                    self.registry.mark_unhealthy(channel.name)
                results.append(result)
                self._log_result(result, message.content_type)
                if result.status == "success" and not fan_out:
                    break
            except Exception as e:
                self.registry.mark_unhealthy(channel.name)
                result = DeliveryResult(
                    message_id=message.message_id, channel=channel.name,
                    status="failed", error_msg=str(e),
                )
                results.append(result)
                self._log_result(result, message.content_type)
        # P0-2 ①: 链尾全败 (含无健康通道/无路由) → 落 outbox, 晨报水位可见
        if not any(r.status == "success" for r in results):
            self._land_outbox(message, channels, results)
        else:
            # B席 ⑤: 至少一条通道成功 → 落段留痕 (整轮一次, 不按通道重复)
            self._log_sections(message)
        return results

    def _land_outbox(self, message: Message, route: list[str],
                     results: list[DeliveryResult]):
        """链尾全败落地; 吞掉一切异常, 绝不影响投递返回值."""
        try:
            path = self.outbox.land(message, route, results)
        except Exception as e:
            logger.warning(f"outbox 落地异常(不影响投递): {e}")
            return
        if path is not None:
            logger.warning(
                f"链尾全败: 消息 {message.message_id} ({message.content_type}) "
                f"已落 outbox {path}"
            )

    @catch_quantstar_error(layer_name="delivery")
    @with_timeout(timeout_s=30)
    def update(self, remote_message_id: str, content: dict,
               channel_name: str = "feishu_dm") -> DeliveryResult:
        """R3/D3: 原地更新已发卡片 (触价卡 PATCH 语义, 范式 4)。

        不走 router/rate_limiter — 更新的是已投递消息, 非新消息。
        通道无 patch 能力/更新失败返回 failed, 调用方降级重发新卡。
        """
        channel = self.registry.get(channel_name)
        if channel is None or not hasattr(channel, "patch"):
            return DeliveryResult(
                message_id=remote_message_id, channel=channel_name,
                status="failed", error_msg="通道不存在或无原地更新能力")
        try:
            result = channel.patch(remote_message_id, content)
            self._log_result(result, "card_update")
            return result
        except Exception as e:
            result = DeliveryResult(
                message_id=remote_message_id, channel=channel_name,
                status="failed", error_msg=str(e))
            self._log_result(result, "card_update")
            return result

    @catch_quantstar_error(layer_name="delivery")
    @with_timeout(timeout_s=30)
    def send_async(self, message: Message, report_mode: str = "debug") -> Future:
        return self._executor.submit(self.send, message, report_mode)

    def _wait_pending(self, timeout: float = 5.0):
        self._executor.shutdown(wait=True)

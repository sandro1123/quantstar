import time

from common.logging import get_logger
from delivery_layer.channels.base import Channel

logger = get_logger("delivery.registry")


class ChannelRegistry:
    def __init__(self):
        self._channels: dict[str, Channel] = {}
        self._healthy: dict[str, bool] = {}
        self._last_retry: dict[str, float] = {}

    def register(self, channel: Channel):
        self._channels[channel.name] = channel
        self._healthy[channel.name] = channel.health()

    def get(self, name: str) -> Channel | None:
        return self._channels.get(name)

    def get_healthy(self, names: list[str]) -> list[Channel]:
        result = []
        for name in names:
            channel = self._channels.get(name)
            if not channel:
                continue
            if not self._healthy.get(name, True):
                if time.time() - self._last_retry.get(name, 0) > 300:
                    if channel.health():
                        self._healthy[name] = True
                        result.append(channel)
                    else:
                        self._last_retry[name] = time.time()
                        logger.warning(f"渠道 {name} 熔断中, 跳过")
                else:
                    logger.info(f"渠道 {name} 熔断中 (距上次重试 <300s), 跳过")
                continue
            result.append(channel)
        return result

    def mark_healthy(self, name: str):
        self._healthy[name] = True

    def mark_unhealthy(self, name: str):
        self._healthy[name] = False
        self._last_retry[name] = time.time()

    def count(self) -> int:
        return len(self._channels)

    def healthy_count(self) -> int:
        return sum(1 for v in self._healthy.values() if v)

    def get_all(self) -> list[Channel]:
        return list(self._channels.values())

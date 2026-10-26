from abc import ABC, abstractmethod
from dataclasses import dataclass

from delivery_layer.message import Message


@dataclass
class DeliveryResult:
    message_id: str
    channel: str
    status: str
    error_msg: str = ""
    duration_ms: int = 0
    retry_count: int = 0
    # HITL: 渠道侧返回的远端消息 id (飞书 message_id)。message_id 字段是内部
    # uuid, 回调要 PATCH 原卡片必须拿远端 id, 故另开一列, 不动既有语义。
    remote_message_id: str = ""


class Channel(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def send(self, message: Message) -> DeliveryResult:
        ...

    @abstractmethod
    def health(self) -> bool:
        ...

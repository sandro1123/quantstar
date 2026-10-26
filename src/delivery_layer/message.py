from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4


@dataclass
class Message:
    content: dict
    content_type: str
    title: str
    severity: str = "info"
    loudness: str = "on"
    force_channel: str | None = None
    force_dm: bool = False
    source: str = "unknown"
    source_version: str | None = None
    message_id: str = field(default_factory=lambda: uuid4().hex[:12])
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

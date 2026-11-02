from __future__ import annotations

from typing import Any


def build_line_event_id(event: dict[str, Any]) -> str:
    webhook_event_id = str(event.get("webhookEventId", "") or "").strip()
    if webhook_event_id:
        return webhook_event_id
    timestamp = str(event.get("timestamp", "") or "").strip()
    message_id = str(event.get("message", {}).get("id", "") or "").strip()
    event_type = str(event.get("type", "") or "").strip()
    return ":".join(part for part in (timestamp, event_type, message_id) if part)

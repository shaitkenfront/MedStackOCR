from __future__ import annotations

from typing import Any

MAX_QUICK_REPLY_ITEMS = 13


def postback_action(label: str, data: str, display_text: str | None = None) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": "postback",
        "label": label[:20],
        "data": data[:300],
    }
    if display_text:
        action["displayText"] = display_text[:300]
    return action


def message_action(label: str, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "label": label[:20],
        "text": text[:300],
    }


def with_quick_reply(text: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
    items = [{"type": "action", "action": action} for action in actions[:MAX_QUICK_REPLY_ITEMS]]
    message: dict[str, Any] = {
        "type": "text",
        "text": text[:5000],
    }
    if items:
        message["quickReply"] = {"items": items}
    return message


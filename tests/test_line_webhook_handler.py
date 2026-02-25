from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from inbox.repository import InboxRepository
from linebot.webhook_handler import LineWebhookHandler


class _DummyReplyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def reply(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        self.calls.append((reply_token, messages))


class LineWebhookHandlerTest(unittest.TestCase):
    def test_handle_invalid_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(tmp)
            handler = LineWebhookHandler(
                config=config,
                reply_client=_DummyReplyClient(),
                repository=InboxRepository(config["inbox"]["sqlite_path"]),
            )
            status, payload = handler.handle(body=b'{"events":[]}', signature="invalid")
            self.assertEqual(status, 401)
            self.assertFalse(payload["ok"])

    def test_handle_help_command_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(tmp)
            reply_client = _DummyReplyClient()
            handler = LineWebhookHandler(
                config=config,
                reply_client=reply_client,
                repository=InboxRepository(config["inbox"]["sqlite_path"]),
            )
            event_payload = {
                "events": [
                    {
                        "type": "message",
                        "webhookEventId": "evt-1",
                        "replyToken": "reply-token",
                        "timestamp": 1,
                        "source": {"type": "user", "userId": "U1"},
                        "message": {"id": "m1", "type": "text", "text": "ヘルプ"},
                    }
                ]
            }
            body = json.dumps(event_payload, ensure_ascii=False).encode("utf-8")
            signature = _signature(config["line_messaging"]["channel_secret"], body)
            status, payload = handler.handle(body=body, signature=signature)
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(len(reply_client.calls), 1)
            _, messages = reply_client.calls[0]
            self.assertIn("使い方", messages[0]["text"])


def _build_config(tmp_dir: str) -> dict[str, Any]:
    return {
        "line_messaging": {
            "enabled": True,
            "channel_secret": "secret",
            "channel_access_token": "token",
            "timeout_sec": 1,
            "allowed_user_ids": [],
        },
        "inbox": {
            "sqlite_path": str(Path(tmp_dir) / "linebot.db"),
            "image_store_dir": str(Path(tmp_dir) / "images"),
            "session_ttl_minutes": 60,
            "max_candidate_options": 3,
            "enable_text_commands": True,
        },
        "ocr": {"engine": "documentai", "engines": {"documentai": {"enabled": True}}, "allowed_engines": ["documentai"]},
        "templates": {"store_path": str(Path(tmp_dir) / "templates"), "household_match_threshold": 0.65},
        "family_registry": {"required": False, "members": []},
        "pipeline": {"review_threshold": 0.72, "reject_threshold": 0.35, "candidate_threshold": 2.5},
    }


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


if __name__ == "__main__":
    unittest.main()


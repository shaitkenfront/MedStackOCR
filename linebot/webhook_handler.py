from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.pipeline import ReceiptExtractionPipeline
from inbox.aggregate_service import AggregateService
from inbox.conversation_service import ConversationService
from inbox.repository_factory import create_inbox_repository
from inbox.repository_interface import InboxRepositoryProtocol
from inbox.retention import cleanup_expired_images
from linebot.event_ids import build_line_event_id
from linebot import message_templates
from linebot.media_client import LineMediaApiClient, guess_extension
from linebot.reply_client import LineReplyClient
from linebot.signature import verify_line_signature
from resolver.year_consistency import apply_year_consistency


class LineWebhookHandler:
    def __init__(
        self,
        config: dict[str, Any],
        media_client: LineMediaApiClient | None = None,
        reply_client: LineReplyClient | None = None,
        repository: InboxRepositoryProtocol | None = None,
    ) -> None:
        self.config = config
        self.line_conf = config.get("line_messaging", {})
        self.inbox_conf = config.get("inbox", {})
        self.enabled = bool(self.line_conf.get("enabled", False))
        self.channel_secret = str(self.line_conf.get("channel_secret", "") or "").strip()
        self.channel_access_token = str(self.line_conf.get("channel_access_token", "") or "").strip()
        self.timeout_sec = float(self.line_conf.get("timeout_sec", 10))
        allowed = self.line_conf.get("allowed_user_ids", [])
        self.allowed_user_ids = {
            str(user_id).strip()
            for user_id in (allowed if isinstance(allowed, list) else [])
            if str(user_id).strip()
        }

        self.repository = repository or create_inbox_repository(config)
        self.aggregate_service = AggregateService(self.repository)
        self.conversation_service = ConversationService(
            repository=self.repository,
            session_ttl_minutes=int(self.inbox_conf.get("session_ttl_minutes", 60)),
            max_candidate_options=int(self.inbox_conf.get("max_candidate_options", 3)),
        )

        api_base_url = str(self.line_conf.get("api_base_url", "https://api.line.me"))
        data_api_base_url = str(self.line_conf.get("data_api_base_url", "https://api-data.line.me"))
        self.media_client = media_client or LineMediaApiClient(
            channel_access_token=self.channel_access_token,
            data_api_base_url=data_api_base_url,
            timeout_sec=self.timeout_sec,
        )
        self.reply_client = reply_client or LineReplyClient(
            channel_access_token=self.channel_access_token,
            api_base_url=api_base_url,
            timeout_sec=self.timeout_sec,
        )

        self.image_store_dir = Path(str(self.inbox_conf.get("image_store_dir", "data/inbox/images")))
        self.image_store_dir.mkdir(parents=True, exist_ok=True)
        self.default_household_id = self.line_conf.get("default_household_id")
        self._pipeline: ReceiptExtractionPipeline | None = None
        self._runtime_config: dict[str, Any] = config

    def handle(self, body: bytes, signature: str | None) -> tuple[int, dict[str, Any]]:
        if not self.enabled:
            return 503, {"ok": False, "error": "line_messaging.enabled is false"}
        if not verify_line_signature(self.channel_secret, body, signature):
            return 401, {"ok": False, "error": "invalid signature"}

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return 400, {"ok": False, "error": "invalid json payload"}
        events = payload.get("events", [])
        if not isinstance(events, list):
            return 400, {"ok": False, "error": "events must be list"}

        handled = 0
        skipped = 0
        errors: list[str] = []
        for event in events:
            if not isinstance(event, dict):
                skipped += 1
                continue
            event_id = build_line_event_id(event)
            if event_id and not self.repository.mark_event_processed(event_id):
                skipped += 1
                continue
            try:
                consumed = self._handle_event(event)
                if consumed:
                    handled += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        return 200, {"ok": len(errors) == 0, "handled": handled, "skipped": skipped, "errors": errors}

    def _handle_event(self, event: dict[str, Any]) -> bool:
        reply_token = str(event.get("replyToken", "") or "").strip()
        source = event.get("source", {})
        source_type = str(source.get("type", "") or "")
        line_user_id = str(source.get("userId", "") or "").strip()
        if source_type != "user" or not line_user_id:
            self._reply(reply_token, [{"type": "text", "text": "1:1トークのみ対応しています。"}])
            return True

        if self.allowed_user_ids and line_user_id not in self.allowed_user_ids:
            self._reply(reply_token, [{"type": "text", "text": "このアカウントは現在利用できません。"}])
            return True

        event_type = str(event.get("type", "") or "").lower()
        if event_type == "message":
            message = event.get("message", {})
            message_type = str(message.get("type", "") or "").lower()
            if message_type == "image":
                return self._handle_image_event(line_user_id, reply_token, message)
            if message_type == "text":
                return self._handle_text_event(line_user_id, reply_token, str(message.get("text", "") or ""))
            self._reply(reply_token, message_templates.build_unknown_message())
            return True

        if event_type == "postback":
            data = str(event.get("postback", {}).get("data", "") or "")
            messages = self.conversation_service.handle_postback(line_user_id=line_user_id, data=data)
            self._reply(reply_token, messages)
            return True

        return False

    def _handle_image_event(self, line_user_id: str, reply_token: str, message: dict[str, Any]) -> bool:
        message_id = str(message.get("id", "") or "").strip()
        if not message_id:
            self._reply(reply_token, message_templates.build_rejected_message())
            return True

        retention_days = int(self.inbox_conf.get("image_retention_days", 14))
        cleanup_expired_images(str(self.image_store_dir), retention_days)

        content, content_type = self.media_client.download_message_content(message_id)
        ext = guess_extension(content_type)
        receipt_id = str(uuid4())
        image_path = self._save_image(receipt_id, message_id, ext, content)
        image_sha256 = hashlib.sha256(content).hexdigest()

        pipeline = self._get_pipeline()
        engine_name = str(self._runtime_config.get("ocr", {}).get("engine", "documentai"))
        result = pipeline.process(
            image_path=str(image_path),
            household_id=self.default_household_id,
            ocr_engine=engine_name,
        )
        apply_year_consistency([result], self._runtime_config)
        self.repository.save_receipt_result(
            receipt_id=receipt_id,
            line_user_id=line_user_id,
            line_message_id=message_id,
            image_path=str(image_path),
            image_sha256=image_sha256,
            result=result,
        )
        messages = self.conversation_service.handle_new_result(
            line_user_id=line_user_id,
            receipt_id=receipt_id,
            result=result,
        )
        self._reply(reply_token, messages)
        return True

    def _handle_text_event(self, line_user_id: str, reply_token: str, text: str) -> bool:
        enable_text_commands = bool(self.inbox_conf.get("enable_text_commands", True))
        messages: list[dict[str, Any]] | None = None
        if enable_text_commands:
            messages = self.aggregate_service.handle_text_command(line_user_id, text)
        if messages is None:
            messages = self.conversation_service.handle_text(line_user_id, text)
        self._reply(reply_token, messages)
        return True

    def _save_image(self, receipt_id: str, message_id: str, extension: str, content: bytes) -> Path:
        day_dir = self.image_store_dir / _today_utc()
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{receipt_id}_{message_id}{extension}"
        path.write_bytes(content)
        return path

    def _reply(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        if not reply_token:
            return
        try:
            self.reply_client.reply(reply_token=reply_token, messages=messages)
        except Exception as exc:  # noqa: BLE001
            print(f"line-reply-failed: {exc}")

    def _get_pipeline(self) -> ReceiptExtractionPipeline:
        if self._pipeline is None:
            self._pipeline = ReceiptExtractionPipeline(self._runtime_config)
        return self._pipeline

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

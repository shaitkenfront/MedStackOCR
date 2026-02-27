from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import load_config
from app.pipeline import ReceiptExtractionPipeline
from core.enums import FieldName
from inbox.aggregate_service import AggregateService
from inbox.conversation_service import ConversationService
from inbox.repository_factory import create_inbox_repository
from inbox.state_machine import STATE_AWAIT_FREE_TEXT
from linebot import message_templates
from linebot.media_client import LineMediaApiClient, guess_extension
from linebot.reply_client import LineReplyClient
from ocr.base import OCRAdapterError
from resolver.year_consistency import apply_year_consistency

try:
    import boto3  # type: ignore
    from botocore.exceptions import ClientError  # type: ignore
except Exception as exc:  # pragma: no cover - import guard for local env
    boto3 = None
    ClientError = Exception
    _BOTO3_IMPORT_ERROR = exc
else:
    _BOTO3_IMPORT_ERROR = None

_ENTRY_SPLIT_RE = re.compile(r"[\r\n]+")
_ALIAS_SPLIT_RE = re.compile(r"[,\u3001\uFF0C/\uFF0F|]+")
_WHITESPACE_RE = re.compile(r"\s+")
NON_DEDUCTIBLE_DETECTION_ENABLED = False
NON_DEDUCTIBLE_KEYWORDS = (
    "ワクチン",
    "予防接種",
    "健診",
    "健康診断",
    "人間ドック",
    "美容",
    "診断書",
    "文書料",
)
_CURRENCY_AMOUNT_RE = re.compile(
    r"(?:[¥￥]\s*(-?(?:\d{1,3}(?:,\d{3})+|\d+)))|(?:(-?(?:\d{1,3}(?:,\d{3})+|\d+))\s*円)"
)
_PLAIN_AMOUNT_TOKEN_RE = re.compile(r"(?<!\d)-?(?:\d{1,3}(?:,\d{3})+|\d{1,6})(?!\d)")
_CANCEL_LAST_REGISTRATION_KEYWORDS = (
    "取り消し",
    "取消",
    "やり直し",
    "削除",
    "失敗",
)
_FAMILY_REGISTRATION_AWAITING_FIELD = "__family_registration__"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ = context
    failures: list[dict[str, str]] = []
    worker = _get_worker()
    if worker is None:
        for record in event.get("Records", []):
            message_id = str(record.get("messageId", "")).strip()
            if message_id:
                failures.append({"itemIdentifier": message_id})
        print(f"worker-init-failed: boto3 is required: {_BOTO3_IMPORT_ERROR}")
        return {"batchItemFailures": failures}

    for record in event.get("Records", []):
        message_id = str(record.get("messageId", "")).strip()
        try:
            envelope = json.loads(str(record.get("body", "") or "{}"))
            line_event = envelope.get("event")
            if not isinstance(line_event, dict):
                raise ValueError("missing event payload")
            worker.process_event(line_event)
        except Exception as exc:  # noqa: BLE001
            print(f"worker-record-failed message_id={message_id} error={exc}")
            if message_id:
                failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


class LineEventWorker:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.line_conf = config.get("line_messaging", {})
        self.inbox_conf = config.get("inbox", {})
        self.ocr_guard_conf = self.inbox_conf.get("ocr_guard", {})
        self.default_household_id = self.line_conf.get("default_household_id")
        self.allowed_user_ids = {
            str(value).strip()
            for value in self.line_conf.get("allowed_user_ids", [])
            if str(value).strip()
        }

        timeout_sec = float(self.line_conf.get("timeout_sec", 10))
        self.repository = create_inbox_repository(config)
        self.aggregate_service = AggregateService(self.repository)
        self.conversation_service = ConversationService(
            repository=self.repository,
            session_ttl_minutes=int(self.inbox_conf.get("session_ttl_minutes", 60)),
            max_candidate_options=int(self.inbox_conf.get("max_candidate_options", 3)),
        )
        self.pipeline = ReceiptExtractionPipeline(config)
        self.media_client = LineMediaApiClient(
            channel_access_token=str(self.line_conf.get("channel_access_token", "")),
            data_api_base_url=str(self.line_conf.get("data_api_base_url", "https://api-data.line.me")),
            timeout_sec=timeout_sec,
        )
        self.reply_client = LineReplyClient(
            channel_access_token=str(self.line_conf.get("channel_access_token", "")),
            api_base_url=str(self.line_conf.get("api_base_url", "https://api.line.me")),
            timeout_sec=timeout_sec,
        )
        self.receipt_bucket = str(os.getenv("RECEIPT_BUCKET", "")).strip()
        self.receipt_prefix = str(os.getenv("RECEIPT_PREFIX", "raw")).strip("/")
        self._s3_client = None if boto3 is None else boto3.client("s3")

    def process_event(self, event: dict[str, Any]) -> None:
        source = event.get("source", {})
        source_type = str(source.get("type", "") or "")
        line_user_id = str(source.get("userId", "") or "").strip()
        reply_token = str(event.get("replyToken", "") or "").strip()

        if source_type != "user" or not line_user_id:
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=[{"type": "text", "text": "1:1トークのみ対応しています。"}],
            )
            return

        if self.allowed_user_ids and line_user_id not in self.allowed_user_ids:
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=[{"type": "text", "text": "このアカウントは現在利用できません。"}],
            )
            return

        event_type = str(event.get("type", "") or "").lower()
        if event_type == "unfollow":
            self._purge_user_data(line_user_id)
            return

        if event_type == "follow":
            self.repository.ensure_family_registration_started(line_user_id)
            self._ensure_family_registration_session(line_user_id)
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=message_templates.build_family_registration_prompt_message(
                    can_finish=bool(self.repository.list_family_members(line_user_id))
                ),
            )
            return

        is_family_registration_completed = self.repository.is_family_registration_completed(line_user_id)
        if not is_family_registration_completed:
            self.repository.ensure_family_registration_started(line_user_id)
            self._ensure_family_registration_session(line_user_id)
            if event_type == "message":
                message = event.get("message", {})
                message_type = str(message.get("type", "") or "").lower()
                if message_type == "text":
                    self._handle_family_registration_text(
                        line_user_id=line_user_id,
                        reply_token=reply_token,
                        text=str(message.get("text", "") or ""),
                    )
                    return
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=message_templates.build_family_registration_prompt_message(
                    can_finish=bool(self.repository.list_family_members(line_user_id))
                ),
            )
            return

        if event_type == "message":
            message = event.get("message", {})
            message_type = str(message.get("type", "") or "").lower()
            if message_type == "image":
                self._handle_image_event(line_user_id=line_user_id, reply_token=reply_token, message=message)
                return
            if message_type == "text":
                self._handle_text_event(
                    line_user_id=line_user_id,
                    reply_token=reply_token,
                    text=str(message.get("text", "") or ""),
                )
                return
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=message_templates.build_unknown_message(),
            )
            return

        if event_type == "postback":
            data = str(event.get("postback", {}).get("data", "") or "")
            duplicate_messages = self._handle_duplicate_postback(line_user_id=line_user_id, data=data)
            if duplicate_messages is not None:
                self._reply(line_user_id=line_user_id, reply_token=reply_token, messages=duplicate_messages)
                return
            messages = self.conversation_service.handle_postback(line_user_id=line_user_id, data=data)
            self._reply(line_user_id=line_user_id, reply_token=reply_token, messages=messages)
            return

    def _handle_image_event(self, line_user_id: str, reply_token: str, message: dict[str, Any]) -> None:
        message_id = str(message.get("id", "") or "").strip()
        if not message_id:
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=message_templates.build_rejected_message(),
            )
            return

        content, content_type = self.media_client.download_message_content(message_id)
        extension = guess_extension(content_type)
        receipt_id = str(uuid4())
        image_sha256 = _sha256_hex(content)
        precheck_duplicates = self.repository.find_potential_duplicates(
            line_user_id=line_user_id,
            image_sha256=image_sha256,
            duplicate_key=None,
            limit=1,
        )
        if precheck_duplicates:
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=message_templates.build_duplicate_image_skipped_message(),
            )
            return

        quota_allowed, quota_reason = self.repository.consume_ocr_quota(
            line_user_id=line_user_id,
            now_utc=datetime.now(timezone.utc),
            user_per_minute_limit=_safe_positive_int(self.ocr_guard_conf.get("user_per_minute"), 3),
            user_per_day_limit=_safe_positive_int(self.ocr_guard_conf.get("user_per_day"), 40),
            global_per_day_limit=_safe_positive_int(self.ocr_guard_conf.get("global_per_day"), 1200),
        ) if _is_true(self.ocr_guard_conf.get("enabled", True)) else (True, None)
        if not quota_allowed:
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=message_templates.build_ocr_quota_exceeded_message(str(quota_reason or "")),
            )
            return

        image_path = self._persist_raw_image(receipt_id=receipt_id, message_id=message_id, extension=extension, content=content)

        temp_path = _write_temp_file(receipt_id=receipt_id, extension=extension, content=content)
        try:
            engine_name = str(self.config.get("ocr", {}).get("engine", "documentai"))
            family_registry_override = self._build_user_family_registry_config(line_user_id)
            try:
                result = self.pipeline.process(
                    image_path=temp_path,
                    household_id=self.default_household_id,
                    ocr_engine=engine_name,
                    family_registry_override=family_registry_override,
                )
            except OCRAdapterError as exc:
                print(f"ocr-adapter-error line_user_id={line_user_id} message_id={message_id} error={exc}")
                self._reply(
                    line_user_id=line_user_id,
                    reply_token=reply_token,
                    messages=message_templates.build_ocr_unavailable_message(),
                )
                return
            extracted_fields = _fields_from_result(result)
            duplicate_key = _build_duplicate_key(extracted_fields)
            duplicates = self.repository.find_potential_duplicates(
                line_user_id=line_user_id,
                image_sha256=image_sha256,
                duplicate_key=duplicate_key,
                limit=3,
            )
            non_deductible_keywords: list[str] = []
            if NON_DEDUCTIBLE_DETECTION_ENABLED:
                non_deductible_keywords = _detect_non_deductible_keywords(result)
            apply_year_consistency([result], self.config)
            self.repository.save_receipt_result(
                receipt_id=receipt_id,
                line_user_id=line_user_id,
                line_message_id=message_id,
                image_path=image_path,
                image_sha256=image_sha256,
                result=result,
            )
            messages = self.conversation_service.handle_new_result(
                line_user_id=line_user_id,
                receipt_id=receipt_id,
                result=result,
            )
            if duplicates:
                messages.extend(message_templates.build_duplicate_warning_message(receipt_id=receipt_id, duplicates=duplicates))
            if non_deductible_keywords:
                messages.extend(message_templates.build_non_deductible_warning_message(non_deductible_keywords))
            self._reply(line_user_id=line_user_id, reply_token=reply_token, messages=messages)
        finally:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass

    def _handle_text_event(self, line_user_id: str, reply_token: str, text: str) -> None:
        if _is_cancel_last_registration_command(text):
            messages = self._cancel_latest_registration(line_user_id)
            self._reply(line_user_id=line_user_id, reply_token=reply_token, messages=messages)
            return

        active_session = self.repository.get_active_session(line_user_id)
        if (
            active_session is not None
            and active_session.state == STATE_AWAIT_FREE_TEXT
            and str(active_session.awaiting_field or "") == _FAMILY_REGISTRATION_AWAITING_FIELD
        ):
            messages = self.conversation_service.handle_text(line_user_id, text)
            self._reply(line_user_id=line_user_id, reply_token=reply_token, messages=messages)
            return

        enable_text_commands = bool(self.inbox_conf.get("enable_text_commands", True))
        messages: list[dict[str, Any]] | None = None
        if enable_text_commands:
            messages = self.aggregate_service.handle_text_command(line_user_id, text)
        if messages is None:
            messages = self.conversation_service.handle_text(line_user_id, text)
        self._reply(line_user_id=line_user_id, reply_token=reply_token, messages=messages)

    def _handle_family_registration_text(self, line_user_id: str, reply_token: str, text: str) -> None:
        self._ensure_family_registration_session(line_user_id)
        messages = self.conversation_service.handle_text(line_user_id, text)
        self._reply(
            line_user_id=line_user_id,
            reply_token=reply_token,
            messages=messages,
        )

    def _ensure_family_registration_session(self, line_user_id: str) -> None:
        active_session = self.repository.get_active_session(line_user_id)
        if (
            active_session is not None
            and active_session.state == STATE_AWAIT_FREE_TEXT
            and str(active_session.awaiting_field or "") == _FAMILY_REGISTRATION_AWAITING_FIELD
        ):
            return
        if active_session is not None:
            self.repository.delete_session(active_session.session_id)
        self.repository.upsert_session(
            line_user_id=line_user_id,
            receipt_id=_FAMILY_REGISTRATION_AWAITING_FIELD,
            state=STATE_AWAIT_FREE_TEXT,
            payload={},
            expires_at=self._session_expires_at(),
            awaiting_field=_FAMILY_REGISTRATION_AWAITING_FIELD,
        )

    def _session_expires_at(self) -> str:
        ttl_minutes = max(5, int(self.inbox_conf.get("session_ttl_minutes", 60)))
        return (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()

    def _build_user_family_registry_config(self, line_user_id: str) -> dict[str, Any]:
        members = self.repository.list_family_members(line_user_id)
        return {
            "required": bool(members),
            "members": members,
        }

    def _purge_user_data(self, line_user_id: str) -> None:
        image_paths = self.repository.purge_user_data(line_user_id)
        deleted = 0
        for image_path in image_paths:
            if self._delete_stored_image(image_path):
                deleted += 1
        print(f"user-data-purged line_user_id={line_user_id} records_images={len(image_paths)} deleted_images={deleted}")

    def _delete_stored_image(self, image_path: str) -> bool:
        path_text = str(image_path or "").strip()
        if not path_text:
            return False

        if path_text.startswith("s3://"):
            bucket, key = _parse_s3_uri(path_text)
            if not bucket or not key or self._s3_client is None:
                return False
            self._s3_client.delete_object(Bucket=bucket, Key=key)
            return True

        local_path = Path(path_text)
        local_path.unlink(missing_ok=True)
        return True

    def _handle_duplicate_postback(self, *, line_user_id: str, data: str) -> list[dict[str, Any]] | None:
        params = _parse_postback_data(data)
        action = str(params.get("a", "") or "").strip()
        receipt_id = str(params.get("r", "") or "").strip()
        if action == "dup_keep":
            return message_templates.build_duplicate_kept_message()
        if action != "dup_del":
            return None
        if not receipt_id:
            return message_templates.build_unknown_message()
        image_path = self.repository.delete_receipt(line_user_id=line_user_id, receipt_id=receipt_id)
        if image_path:
            self._delete_stored_image(image_path)
        return message_templates.build_duplicate_deleted_message()

    def _cancel_latest_registration(self, line_user_id: str) -> list[dict[str, Any]]:
        receipt_id = self.repository.get_latest_receipt_id(line_user_id)
        if not receipt_id:
            return message_templates.build_last_registration_not_found_message()
        image_path = self.repository.delete_receipt(line_user_id=line_user_id, receipt_id=receipt_id)
        if image_path is None:
            return message_templates.build_last_registration_not_found_message()
        self._delete_stored_image(image_path)
        return message_templates.build_last_registration_cancelled_message()

    def _persist_raw_image(self, receipt_id: str, message_id: str, extension: str, content: bytes) -> str:
        if self.receipt_bucket:
            if self._s3_client is None:
                raise RuntimeError("boto3 is required to use S3 image storage")
            now = datetime.now(timezone.utc)
            key = (
                f"{self.receipt_prefix}/{now.strftime('%Y/%m/%d')}/{receipt_id}_{message_id}{extension}"
                if self.receipt_prefix
                else f"{now.strftime('%Y/%m/%d')}/{receipt_id}_{message_id}{extension}"
            )
            content_type = _content_type_from_extension(extension)
            self._s3_client.put_object(
                Bucket=self.receipt_bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
            )
            return f"s3://{self.receipt_bucket}/{key}"

        fallback_path = Path(tempfile.gettempdir()) / f"{receipt_id}_{message_id}{extension}"
        fallback_path.write_bytes(content)
        return str(fallback_path)

    def _reply(self, line_user_id: str, reply_token: str, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        if reply_token:
            try:
                self.reply_client.reply(reply_token=reply_token, messages=messages)
                return
            except Exception as exc:  # noqa: BLE001
                print(f"line-reply-failed fallback-to-push error={exc}")
        if line_user_id:
            self.reply_client.push(to=line_user_id, messages=messages)


_worker_instance: LineEventWorker | None = None
_cached_app_secret_values: dict[str, Any] | None = None


def _get_worker() -> LineEventWorker | None:
    global _worker_instance
    if _worker_instance is not None:
        return _worker_instance
    if boto3 is None:
        return None
    config = load_config(os.getenv("CONFIG_PATH", "config.yaml"))
    _apply_secret_overrides(config)
    _apply_env_overrides(config)
    _inject_docai_credentials_from_env(config)
    _worker_instance = LineEventWorker(config)
    return _worker_instance


def _apply_env_overrides(config: dict[str, Any]) -> None:
    line_conf = config.setdefault("line_messaging", {})
    ocr_conf = config.setdefault("ocr", {})
    engines = ocr_conf.setdefault("engines", {})
    docai_conf = engines.setdefault("documentai", {})
    templates_conf = config.setdefault("templates", {})
    inbox_conf = config.setdefault("inbox", {})
    ddb_conf = inbox_conf.setdefault("dynamodb", {})
    ddb_tables = ddb_conf.setdefault("tables", {})
    guard_conf = inbox_conf.setdefault("ocr_guard", {})

    mapping = {
        "LINE_CHANNEL_SECRET": (line_conf, "channel_secret"),
        "LINE_CHANNEL_ACCESS_TOKEN": (line_conf, "channel_access_token"),
        "LINE_DEFAULT_HOUSEHOLD_ID": (line_conf, "default_household_id"),
        "DOC_AI_PROJECT_ID": (docai_conf, "project_id"),
        "DOC_AI_LOCATION": (docai_conf, "location"),
        "DOC_AI_PROCESSOR_ID": (docai_conf, "processor_id"),
        "DOC_AI_PROCESSOR_VERSION": (docai_conf, "processor_version"),
        "DOC_AI_ENDPOINT": (docai_conf, "endpoint"),
        "DOC_AI_CREDENTIALS_PATH": (docai_conf, "credentials_path"),
        "DDB_REGION": (ddb_conf, "region"),
        "DDB_TABLE_PREFIX": (ddb_conf, "table_prefix"),
        "DDB_EVENT_TABLE": (ddb_tables, "event_dedupe"),
        "DDB_RECEIPTS_TABLE": (ddb_tables, "receipts"),
        "DDB_FIELDS_TABLE": (ddb_tables, "receipt_fields"),
        "DDB_SESSIONS_TABLE": (ddb_tables, "sessions"),
        "DDB_AGGREGATE_TABLE": (ddb_tables, "aggregate_entries"),
        "DDB_FAMILY_TABLE": (ddb_tables, "family_registry"),
        "DDB_LEARNING_TABLE": (ddb_tables, "learning_rules"),
        "DDB_OCR_GUARD_TABLE": (ddb_tables, "ocr_usage_guard"),
    }
    for env_name, (target, key) in mapping.items():
        value = os.getenv(env_name)
        if value is not None and value != "":
            target[key] = value

    template_store_path = str(os.getenv("TEMPLATE_STORE_PATH", "") or "").strip()
    if template_store_path:
        templates_conf["store_path"] = template_store_path
    elif os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        # Lambda では /var/task が read-only のため、テンプレート保存先を /tmp に寄せる。
        templates_conf["store_path"] = "/tmp/data/templates"

    allowed_ids = os.getenv("LINE_ALLOWED_USER_IDS", "")
    if allowed_ids.strip():
        line_conf["allowed_user_ids"] = [item.strip() for item in allowed_ids.split(",") if item.strip()]

    backend = os.getenv("INBOX_BACKEND", "").strip().lower()
    if backend:
        inbox_conf["backend"] = backend

    if "OCR_GUARD_ENABLED" in os.environ:
        guard_conf["enabled"] = _is_true(os.getenv("OCR_GUARD_ENABLED", ""))
    if os.getenv("OCR_GUARD_USER_PER_MINUTE", "").strip():
        guard_conf["user_per_minute"] = int(os.getenv("OCR_GUARD_USER_PER_MINUTE", "3"))
    if os.getenv("OCR_GUARD_USER_PER_DAY", "").strip():
        guard_conf["user_per_day"] = int(os.getenv("OCR_GUARD_USER_PER_DAY", "40"))
    if os.getenv("OCR_GUARD_GLOBAL_PER_DAY", "").strip():
        guard_conf["global_per_day"] = int(os.getenv("OCR_GUARD_GLOBAL_PER_DAY", "1200"))


def _apply_secret_overrides(config: dict[str, Any]) -> None:
    secret_values = _load_app_secret_values()
    if not secret_values:
        return

    line_conf = config.setdefault("line_messaging", {})
    doc_conf = config.setdefault("ocr", {}).setdefault("engines", {}).setdefault("documentai", {})

    line_secret = str(secret_values.get("line_channel_secret", "") or "").strip()
    line_token = str(secret_values.get("line_channel_access_token", "") or "").strip()
    docai_credentials_json = secret_values.get("docai_credentials_json")
    docai_credentials_path = str(secret_values.get("docai_credentials_path", "") or "").strip()

    if line_secret:
        line_conf["channel_secret"] = line_secret
    if line_token:
        line_conf["channel_access_token"] = line_token
    if isinstance(docai_credentials_json, (str, dict)):
        doc_conf["credentials_json"] = docai_credentials_json
    if docai_credentials_path:
        doc_conf["credentials_path"] = docai_credentials_path


def _load_app_secret_values() -> dict[str, Any]:
    global _cached_app_secret_values
    if _cached_app_secret_values is not None:
        return _cached_app_secret_values
    if boto3 is None:
        _cached_app_secret_values = {}
        return _cached_app_secret_values
    secret_id = str(os.getenv("APP_SECRETS_ARN", "") or "").strip() or str(
        os.getenv("APP_SECRETS_NAME", "") or ""
    ).strip()
    if not secret_id:
        _cached_app_secret_values = {}
        return _cached_app_secret_values
    client = boto3.client("secretsmanager")
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        raise RuntimeError(f"failed to read app secret from Secrets Manager: {exc}") from exc
    raw = response.get("SecretString")
    if not isinstance(raw, str) or not raw.strip():
        _cached_app_secret_values = {}
        return _cached_app_secret_values
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"invalid app secret JSON payload: {exc}") from exc
    _cached_app_secret_values = parsed if isinstance(parsed, dict) else {}
    return _cached_app_secret_values


def _inject_docai_credentials_from_env(config: dict[str, Any]) -> None:
    payload = str(os.getenv("DOC_AI_CREDENTIALS_JSON", "") or "").strip()
    if not payload:
        doc_conf = (
            config.get("ocr", {})
            .get("engines", {})
            .get("documentai", {})
        )
        configured_payload = doc_conf.get("credentials_json") if isinstance(doc_conf, dict) else None
        if isinstance(configured_payload, dict):
            payload = json.dumps(configured_payload, ensure_ascii=False)
        elif isinstance(configured_payload, str):
            payload = configured_payload.strip()
    if not payload:
        return

    text = payload
    if not payload.lstrip().startswith("{"):
        try:
            text = base64.b64decode(payload).decode("utf-8")
        except Exception:
            text = payload

    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise RuntimeError(f"invalid DOC_AI_CREDENTIALS_JSON: {exc}") from exc

    credentials_obj: dict[str, Any] | None = None
    if isinstance(parsed, dict) and "type" in parsed:
        credentials_obj = parsed
    elif isinstance(parsed, dict):
        wrapped = parsed.get("credentials_json")
        if isinstance(wrapped, str) and wrapped.strip():
            try:
                nested = json.loads(wrapped)
            except Exception as exc:
                raise RuntimeError(f"invalid nested credentials_json: {exc}") from exc
            if isinstance(nested, dict):
                credentials_obj = nested

    if not credentials_obj:
        raise RuntimeError("DOC_AI_CREDENTIALS_JSON must be a service account JSON object")

    temp_path = Path(tempfile.gettempdir()) / "docai_credentials.json"
    temp_path.write_text(json.dumps(credentials_obj, ensure_ascii=False), encoding="utf-8")

    doc_conf = (
        config.setdefault("ocr", {})
        .setdefault("engines", {})
        .setdefault("documentai", {})
    )
    doc_conf["credentials_path"] = str(temp_path)


def _parse_family_registration_entries(text: str) -> list[tuple[str, list[str]]]:
    rows = [row.strip() for row in _ENTRY_SPLIT_RE.split(str(text or "")) if row.strip()]
    entries: list[tuple[str, list[str]]] = []
    for row in rows:
        parts = [_normalize_family_name(part) for part in _ALIAS_SPLIT_RE.split(row)]
        names = [name for name in parts if name]
        if not names:
            continue
        canonical_name = names[0]
        aliases = _dedupe_family_names(names[1:])
        entries.append((canonical_name, aliases))
    return entries


def _is_cancel_last_registration_command(text: str) -> bool:
    normalized = _normalize_cancel_command_text(text)
    if not normalized:
        return False
    if normalized in _CANCEL_LAST_REGISTRATION_KEYWORDS:
        return True
    if len(normalized) > 12:
        return False
    for keyword in _CANCEL_LAST_REGISTRATION_KEYWORDS:
        if keyword in normalized:
            return True
    return False


def _normalize_cancel_command_text(text: str) -> str:
    normalized = str(text or "").replace("\u3000", " ").strip().lower()
    if not normalized:
        return ""
    compact = _WHITESPACE_RE.sub("", normalized)
    return compact


def _is_true(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(value)


def _safe_positive_int(value: Any, default: int) -> int:
    try:
        resolved = int(value)
    except Exception:
        return default
    return resolved if resolved > 0 else default


def _normalize_family_name(value: str) -> str:
    text = str(value or "").replace("\u3000", " ").strip()
    if not text:
        return ""
    # 全角/半角スペースを含む連続空白を、半角スペース1個に正規化する。
    return _WHITESPACE_RE.sub(" ", text)


def _collect_missing_name_separator_canonicals(entries: list[tuple[str, list[str]]]) -> list[str]:
    invalid: list[str] = []
    for canonical_name, _ in entries:
        if _has_family_given_space(canonical_name):
            continue
        if canonical_name not in invalid:
            invalid.append(canonical_name)
    return invalid


def _has_family_given_space(name: str) -> bool:
    normalized = _normalize_family_name(name)
    if not normalized or " " not in normalized:
        return False
    parts = normalized.split(" ")
    return len(parts) >= 2 and bool(parts[0]) and bool(parts[1])


def _dedupe_family_names(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_family_name(value)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _parse_postback_data(data: str) -> dict[str, str]:
    parsed = parse_qs(str(data or ""), keep_blank_values=True)
    output: dict[str, str] = {}
    for key, values in parsed.items():
        if not values:
            continue
        output[key] = values[0]
    return output


def _fields_from_result(result: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    raw_fields = getattr(result, "fields", {})
    if not isinstance(raw_fields, dict):
        return fields
    for field_name, candidate in raw_fields.items():
        if candidate is None:
            continue
        value = getattr(candidate, "value_normalized", None)
        if value in (None, ""):
            value = getattr(candidate, "value_raw", None)
        fields[str(field_name)] = value
    return fields


def _build_duplicate_key(fields: dict[str, Any]) -> str | None:
    date_text = _to_date_text(fields.get(FieldName.PAYMENT_DATE))
    amount = _to_int(fields.get(FieldName.PAYMENT_AMOUNT))
    family_name = _normalize_duplicate_text(fields.get(FieldName.FAMILY_MEMBER_NAME))
    facility = _normalize_duplicate_text(fields.get(FieldName.PAYER_FACILITY_NAME))
    if not facility:
        facility = _normalize_duplicate_text(fields.get(FieldName.PRESCRIBING_FACILITY_NAME))
    if not date_text or amount is None or not family_name or not facility:
        return None
    return f"{date_text}|{facility}|{family_name}|{amount}"


def _normalize_duplicate_text(value: Any) -> str:
    text = str(value or "").replace("\u3000", " ").strip().lower()
    if not text:
        return ""
    return "".join(ch for ch in text if ch.isalnum() or ch in {"-", "_"})


def _detect_non_deductible_keywords(result: Any) -> list[str]:
    lines = getattr(result, "ocr_lines", [])
    if not isinstance(lines, list):
        return []

    found: list[str] = []
    for line in lines:
        text = str(getattr(line, "text", "") or "")
        if not text:
            continue
        lowered = text.lower()
        matched_keywords = [keyword for keyword in NON_DEDUCTIBLE_KEYWORDS if keyword.lower() in lowered]
        if not matched_keywords:
            continue

        # 項目名だけの列見出しは誤検知しやすいため、金額の存在を必須条件にする。
        score = _keyword_line_amount_score(text)
        if score < 2:
            score += _same_row_amount_score(lines=lines, target_line=line)
        if score < 2:
            continue

        for keyword in matched_keywords:
            if keyword not in found:
                found.append(keyword)
    return found


def _keyword_line_amount_score(text: str) -> int:
    if _extract_currency_positive_amounts(text):
        return 2
    if _extract_plain_positive_amounts(text):
        return 1
    return 0


def _same_row_amount_score(lines: list[Any], target_line: Any) -> int:
    target_bbox = _to_bbox_tuple(getattr(target_line, "bbox", None))
    if target_bbox is None:
        return 0
    target_page = _to_page_number(target_line)
    target_x = (target_bbox[0] + target_bbox[2]) / 2.0
    target_y = (target_bbox[1] + target_bbox[3]) / 2.0

    best = 0
    for line in lines:
        if line is target_line:
            continue
        if _to_page_number(line) != target_page:
            continue
        text = str(getattr(line, "text", "") or "").strip()
        if not text:
            continue
        bbox = _to_bbox_tuple(getattr(line, "bbox", None))
        if bbox is None:
            continue

        x = (bbox[0] + bbox[2]) / 2.0
        y = (bbox[1] + bbox[3]) / 2.0
        if abs(y - target_y) > 0.03:
            continue
        if x <= target_x:
            continue

        if _extract_currency_positive_amounts(text):
            return 2
        if _looks_like_amount_cell_text(text) and _extract_plain_positive_amounts(text):
            best = max(best, 1)
    return best


def _extract_currency_positive_amounts(text: str) -> list[int]:
    values: list[int] = []
    for match in _CURRENCY_AMOUNT_RE.finditer(str(text or "")):
        token = (match.group(1) or match.group(2) or "").strip()
        value = _parse_integer_token(token)
        if value is None or value <= 0:
            continue
        values.append(value)
    return values


def _extract_plain_positive_amounts(text: str) -> list[int]:
    source = str(text or "")
    values: list[int] = []
    for match in _PLAIN_AMOUNT_TOKEN_RE.finditer(source):
        start, end = match.span()
        before = source[start - 1] if start > 0 else ""
        after = source[end] if end < len(source) else ""
        # 日付や時刻など、金額ではない連番を除外する。
        if before in {"/", ":", "-"} or after in {"/", ":", "-"}:
            continue

        token = match.group(0).strip()
        value = _parse_integer_token(token)
        if value is None or value <= 0:
            continue
        # 西暦4桁の単独値は年の可能性が高いため除外する。
        if len(token.replace(",", "").lstrip("-")) == 4 and 1900 <= value <= 2100:
            continue
        values.append(value)
    return values


def _looks_like_amount_cell_text(text: str) -> bool:
    normalized = str(text or "").replace("\u3000", "").replace(" ", "")
    if not normalized:
        return False
    if "/" in normalized or ":" in normalized:
        return False
    if "-" in normalized[1:]:
        return False
    return bool(re.fullmatch(r"[¥￥]?-?(?:\d{1,3}(?:,\d{3})+|\d{1,6})円?", normalized))


def _parse_integer_token(token: str) -> int | None:
    normalized = str(token or "").replace(",", "").strip()
    if not normalized or normalized in {"-", "+", "--"}:
        return None
    try:
        return int(normalized)
    except ValueError:
        return None


def _to_bbox_tuple(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x1 = float(value[0])
        y1 = float(value[1])
        x2 = float(value[2])
        y2 = float(value[3])
    except Exception:
        return None
    return (x1, y1, x2, y2)


def _to_page_number(line: Any) -> int:
    try:
        return int(getattr(line, "page", 1) or 1)
    except Exception:
        return 1


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(str(uri or "").strip())
    if parsed.scheme != "s3":
        return "", ""
    bucket = str(parsed.netloc or "").strip()
    key = str(parsed.path or "").lstrip("/")
    return bucket, key


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip()
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch == "-")
    if not cleaned or cleaned in {"-", "--"}:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _to_date_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 3:
            try:
                return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
            except Exception:
                return text
    return text


def _write_temp_file(receipt_id: str, extension: str, content: bytes) -> str:
    suffix = extension if extension.startswith(".") else f".{extension}"
    path = Path(tempfile.gettempdir()) / f"{receipt_id}{suffix}"
    path.write_bytes(content)
    return str(path)


def _content_type_from_extension(extension: str) -> str:
    normalized = extension.lower()
    if normalized == ".png":
        return "image/png"
    if normalized in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if normalized == ".webp":
        return "image/webp"
    if normalized == ".gif":
        return "image/gif"
    return "application/octet-stream"


def _sha256_hex(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()

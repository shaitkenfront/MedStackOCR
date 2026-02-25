from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import load_config
from app.pipeline import ReceiptExtractionPipeline
from inbox.aggregate_service import AggregateService
from inbox.conversation_service import ConversationService
from inbox.repository_factory import create_inbox_repository
from linebot import message_templates
from linebot.media_client import LineMediaApiClient, guess_extension
from linebot.reply_client import LineReplyClient
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
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=message_templates.build_family_registration_prompt_message(),
            )
            return

        is_family_registration_completed = self.repository.is_family_registration_completed(line_user_id)
        if not is_family_registration_completed:
            self.repository.ensure_family_registration_started(line_user_id)
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
                messages=message_templates.build_family_registration_prompt_message(),
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
        image_path = self._persist_raw_image(receipt_id=receipt_id, message_id=message_id, extension=extension, content=content)

        temp_path = _write_temp_file(receipt_id=receipt_id, extension=extension, content=content)
        try:
            engine_name = str(self.config.get("ocr", {}).get("engine", "documentai"))
            family_registry_override = self._build_user_family_registry_config(line_user_id)
            result = self.pipeline.process(
                image_path=temp_path,
                household_id=self.default_household_id,
                ocr_engine=engine_name,
                family_registry_override=family_registry_override,
            )
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
            self._reply(line_user_id=line_user_id, reply_token=reply_token, messages=messages)
        finally:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass

    def _handle_text_event(self, line_user_id: str, reply_token: str, text: str) -> None:
        enable_text_commands = bool(self.inbox_conf.get("enable_text_commands", True))
        messages: list[dict[str, Any]] | None = None
        if enable_text_commands:
            messages = self.aggregate_service.handle_text_command(line_user_id, text)
        if messages is None:
            messages = self.conversation_service.handle_text(line_user_id, text)
        self._reply(line_user_id=line_user_id, reply_token=reply_token, messages=messages)

    def _handle_family_registration_text(self, line_user_id: str, reply_token: str, text: str) -> None:
        normalized = str(text or "").strip()
        if not normalized:
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=message_templates.build_family_registration_prompt_message(),
            )
            return

        finish_keyword = message_templates.FAMILY_REGISTRATION_FINISH_TEXT
        if normalized == finish_keyword:
            members = self.repository.list_family_members(line_user_id)
            if not members:
                self._reply(
                    line_user_id=line_user_id,
                    reply_token=reply_token,
                    messages=message_templates.build_family_registration_need_member_message(),
                )
                return
            self.repository.complete_family_registration(line_user_id)
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=message_templates.build_family_registration_completed_message(len(members)),
            )
            return

        entries = _parse_family_registration_entries(normalized)
        if not entries:
            self._reply(
                line_user_id=line_user_id,
                reply_token=reply_token,
                messages=message_templates.build_family_registration_prompt_message(),
            )
            return

        latest_names: list[str] = []
        for canonical_name, aliases in entries:
            member_id = self.repository.upsert_family_member(
                line_user_id=line_user_id,
                canonical_name=canonical_name,
                aliases=aliases,
            )
            if member_id:
                latest_names.append(canonical_name)

        members = self.repository.list_family_members(line_user_id)
        self._reply(
            line_user_id=line_user_id,
            reply_token=reply_token,
            messages=message_templates.build_family_registration_saved_message(len(members), latest_names),
        )

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
    inbox_conf = config.setdefault("inbox", {})
    ddb_conf = inbox_conf.setdefault("dynamodb", {})
    ddb_tables = ddb_conf.setdefault("tables", {})

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
    }
    for env_name, (target, key) in mapping.items():
        value = os.getenv(env_name)
        if value is not None and value != "":
            target[key] = value

    allowed_ids = os.getenv("LINE_ALLOWED_USER_IDS", "")
    if allowed_ids.strip():
        line_conf["allowed_user_ids"] = [item.strip() for item in allowed_ids.split(",") if item.strip()]

    backend = os.getenv("INBOX_BACKEND", "").strip().lower()
    if backend:
        inbox_conf["backend"] = backend


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


def _normalize_family_name(value: str) -> str:
    text = str(value or "").replace("\u3000", " ").strip()
    return " ".join(part for part in text.split(" ") if part)


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


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(str(uri or "").strip())
    if parsed.scheme != "s3":
        return "", ""
    bucket = str(parsed.netloc or "").strip()
    key = str(parsed.path or "").lstrip("/")
    return bucket, key


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

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from linebot.event_ids import build_line_event_id
from linebot.signature import verify_line_signature

try:
    import boto3  # type: ignore
    from botocore.exceptions import ClientError  # type: ignore
except Exception as exc:  # pragma: no cover - import guard for local env
    boto3 = None
    ClientError = Exception
    _BOTO3_IMPORT_ERROR = exc
else:
    _BOTO3_IMPORT_ERROR = None


LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
EVENT_DEDUPE_TABLE = os.getenv("EVENT_DEDUPE_TABLE", "")
EVENT_DEDUPE_TTL_DAYS = int(os.getenv("EVENT_DEDUPE_TTL_DAYS", "7"))
LINE_WEBHOOK_PATH = os.getenv("LINE_WEBHOOK_PATH", "/webhook/line")
APP_SECRETS_ARN = os.getenv("APP_SECRETS_ARN", "")
APP_SECRETS_NAME = os.getenv("APP_SECRETS_NAME", "")

_sqs_client = None if boto3 is None else boto3.client("sqs")
_dynamodb_client = None if boto3 is None else boto3.client("dynamodb")
_secrets_client = None if boto3 is None else boto3.client("secretsmanager")
_cached_secret_values: dict[str, Any] | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ = context
    if boto3 is None:
        return _response(500, {"ok": False, "error": f"boto3 is required: {_BOTO3_IMPORT_ERROR}"})
    if not SQS_QUEUE_URL:
        return _response(500, {"ok": False, "error": "SQS_QUEUE_URL is required"})

    method = str(event.get("requestContext", {}).get("http", {}).get("method", "")).upper()
    path = str(event.get("rawPath", ""))
    if method != "POST":
        return _response(405, {"ok": False, "error": "method_not_allowed"})
    if LINE_WEBHOOK_PATH and path and path != LINE_WEBHOOK_PATH:
        return _response(404, {"ok": False, "error": "not_found"})

    body_bytes = _decode_body(event)
    headers = event.get("headers", {})
    signature = _get_header(headers, "x-line-signature")
    channel_secret = _resolve_line_channel_secret()
    if not verify_line_signature(channel_secret, body_bytes, signature):
        return _response(401, {"ok": False, "error": "invalid_signature"})

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return _response(400, {"ok": False, "error": "invalid_json"})

    events = payload.get("events")
    if not isinstance(events, list):
        return _response(400, {"ok": False, "error": "events_must_be_list"})

    enqueued = 0
    skipped = 0
    for line_event in events:
        if not isinstance(line_event, dict):
            skipped += 1
            continue
        event_id = build_line_event_id(line_event)
        if not event_id:
            skipped += 1
            continue
        if not _mark_event(event_id):
            skipped += 1
            continue
        _enqueue_line_event(line_event=line_event, event_id=event_id)
        enqueued += 1

    return _response(200, {"ok": True, "enqueued": enqueued, "skipped": skipped})


def _decode_body(event: dict[str, Any]) -> bytes:
    body = event.get("body", "")
    if body is None:
        return b""
    if bool(event.get("isBase64Encoded", False)):
        return base64.b64decode(str(body))
    return str(body).encode("utf-8")


def _get_header(headers: Any, name: str) -> str | None:
    if not isinstance(headers, dict):
        return None
    needle = name.lower()
    for key, value in headers.items():
        if str(key).lower() == needle:
            return str(value)
    return None


def _mark_event(event_id: str) -> bool:
    if not EVENT_DEDUPE_TABLE:
        return True
    now = datetime.now(timezone.utc)
    expires_at = int((now + timedelta(days=max(1, EVENT_DEDUPE_TTL_DAYS))).timestamp())
    try:
        _dynamodb_client.put_item(
            TableName=EVENT_DEDUPE_TABLE,
            Item={
                "event_id": {"S": event_id},
                "received_at": {"S": now.isoformat()},
                "expires_at_epoch": {"N": str(expires_at)},
            },
            ConditionExpression="attribute_not_exists(event_id)",
        )
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "ConditionalCheckFailedException":
            return False
        raise


def _resolve_line_channel_secret() -> str:
    if LINE_CHANNEL_SECRET:
        return LINE_CHANNEL_SECRET
    secret_values = _load_app_secret_values()
    return str(secret_values.get("line_channel_secret", "") or "")


def _load_app_secret_values() -> dict[str, Any]:
    global _cached_secret_values
    if _cached_secret_values is not None:
        return _cached_secret_values
    secret_id = APP_SECRETS_ARN or APP_SECRETS_NAME
    if not secret_id:
        _cached_secret_values = {}
        return _cached_secret_values
    response = _secrets_client.get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString")
    if not isinstance(raw, str) or not raw.strip():
        _cached_secret_values = {}
        return _cached_secret_values
    try:
        parsed = json.loads(raw)
    except Exception:
        _cached_secret_values = {}
        return _cached_secret_values
    _cached_secret_values = parsed if isinstance(parsed, dict) else {}
    return _cached_secret_values


def _enqueue_line_event(line_event: dict[str, Any], event_id: str) -> None:
    source = line_event.get("source", {})
    line_user_id = str(source.get("userId", "") or "").strip()
    group_id = line_user_id or "line-default"
    envelope = {
        "event_id": event_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "event": line_event,
    }
    _sqs_client.send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps(envelope, ensure_ascii=False),
        MessageGroupId=group_id,
        MessageDeduplicationId=event_id,
    )


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "content-type": "application/json; charset=utf-8",
        },
        "body": json.dumps(payload, ensure_ascii=False),
    }

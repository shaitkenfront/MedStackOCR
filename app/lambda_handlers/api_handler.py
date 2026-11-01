"""LIFF 画面向けの明細 API（API Gateway HTTP API → Lambda）。

OCR 用の重い依存（Document AI など）は読み込まず、DynamoDB と S3 だけで動く。
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

from inbox.dynamo_repository import DynamoInboxRepository
from ledger.api import ApiRequest, LedgerApi
from ledger.auth import LineIdTokenVerifier
from ledger.service import LedgerService

try:
    import boto3  # type: ignore
except Exception:  # pragma: no cover - ローカル環境向けの import ガード
    boto3 = None

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_api_instance: LedgerApi | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    api = _get_api()
    return api.handle(ApiRequest.from_http_api_event(event)).to_http_api_response()


def _get_api() -> LedgerApi:
    global _api_instance
    if _api_instance is None:
        _api_instance = build_api_from_env(os.environ)
    return _api_instance


def build_api_from_env(env: Any, *, dynamodb_resource: Any | None = None, s3_client: Any | None = None) -> LedgerApi:
    repository = DynamoInboxRepository(
        region_name=_optional(env.get("DDB_REGION")),
        table_prefix=str(env.get("DDB_TABLE_PREFIX") or "medstackocr"),
        receipts_table_name=_optional(env.get("DDB_RECEIPTS_TABLE")),
        receipt_fields_table_name=_optional(env.get("DDB_FIELDS_TABLE")),
        sessions_table_name=_optional(env.get("DDB_SESSIONS_TABLE")),
        aggregate_table_name=_optional(env.get("DDB_AGGREGATE_TABLE")),
        family_registry_table_name=_optional(env.get("DDB_FAMILY_TABLE")),
        dynamodb_resource=dynamodb_resource,
    )
    verifier = LineIdTokenVerifier(str(env.get("LINE_LOGIN_CHANNEL_ID") or ""))
    if not verifier.channel_id:
        logger.error("LINE_LOGIN_CHANNEL_ID is not set; every request will be rejected")

    bucket = str(env.get("RECEIPT_BUCKET") or "").strip()
    if s3_client is None and boto3 is not None and bucket:
        s3_client = boto3.client("s3")

    def delete_image(image_path: str) -> None:
        target_bucket, key = _parse_s3_uri(image_path)
        # 自分のバケット以外のパスは消さない（データ不整合で他の場所を消す事故を防ぐ）
        if not key or s3_client is None or target_bucket != bucket:
            logger.warning("skip deleting receipt image outside the receipt bucket")
            return
        s3_client.delete_object(Bucket=target_bucket, Key=key)

    return LedgerApi(LedgerService(repository), verifier, image_deleter=delete_image)


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(str(uri or "").strip())
    if parsed.scheme != "s3":
        return "", ""
    return str(parsed.netloc or "").strip(), str(parsed.path or "").lstrip("/")


def _optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None

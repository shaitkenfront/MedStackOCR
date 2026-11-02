"""LIFF 画面向けの明細 API（API Gateway HTTP API → Lambda）。

OCR 用の重い依存（Document AI など）は読み込まず、DynamoDB と S3 だけで動く。
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any
from uuid import uuid4
from urllib.parse import urlparse

from inbox.dynamo_repository import DynamoInboxRepository
from inbox.ocr_archive import archive_path_for
from ledger.api import EXPORT_URL_EXPIRES_SECONDS, ApiRequest, LedgerApi
from ledger.auth import LineIdTokenVerifier
from ledger.service import LedgerService

try:
    import boto3  # type: ignore
    from botocore.config import Config as BotoConfig  # type: ignore
except Exception:  # pragma: no cover - ローカル環境向けの import ガード
    boto3 = None
    BotoConfig = None

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
        learning_table_name=_optional(env.get("DDB_LEARNING_TABLE")),
        dynamodb_resource=dynamodb_resource,
    )
    verifier = LineIdTokenVerifier(str(env.get("LINE_LOGIN_CHANNEL_ID") or ""))
    if not verifier.channel_id:
        logger.error("LINE_LOGIN_CHANNEL_ID is not set; every request will be rejected")

    bucket = str(env.get("RECEIPT_BUCKET") or "").strip()
    if s3_client is None and boto3 is not None and bucket:
        # 署名付き URL を Lambda と同じリージョンのエンドポイントで発行する（リダイレクトによる署名不一致を避ける）
        s3_client = boto3.client(
            "s3",
            region_name=_optional(env.get("AWS_REGION")),
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}) if BotoConfig else None,
        )

    def delete_image(image_path: str) -> None:
        target_bucket, key = _parse_s3_uri(image_path)
        # 自分のバケット以外のパスは消さない（データ不整合で他の場所を消す事故を防ぐ）
        if not key or s3_client is None or target_bucket != bucket:
            logger.warning("skip deleting receipt image outside the receipt bucket")
            return
        s3_client.delete_object(Bucket=target_bucket, Key=key)
        # 画像と一緒に保存している OCR の読み取り結果も消す
        _, archive_key = _parse_s3_uri(archive_path_for(image_path))
        s3_client.delete_object(Bucket=target_bucket, Key=archive_key)

    def sign_image_url(image_path: str, expires_in: int) -> str | None:
        target_bucket, key = _parse_s3_uri(image_path)
        if not key or s3_client is None or target_bucket != bucket:
            return None
        try:
            s3_client.head_object(Bucket=target_bucket, Key=key)
        except Exception as exc:  # 保存期間を過ぎて消えている場合など
            if _is_not_found(exc):
                return None
            raise
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": target_bucket, "Key": key, "ResponseCacheControl": "private, no-store"},
            ExpiresIn=expires_in,
        )

    export_prefix = str(env.get("EXPORT_PREFIX") or "exports").strip("/")

    def upload_export(user_id: str, filename: str, content: bytes) -> str:
        """申告用の資料を一時的に保存し、期限付きのダウンロード URL を返す（保存先はライフサイクルで1日後に削除）。"""
        if s3_client is None or not bucket:
            raise RuntimeError("receipt bucket is not configured")
        owner = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
        key = f"{export_prefix}/{owner}/{uuid4()}/{filename}"
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content,
            ContentType=XLSX_CONTENT_TYPE,
        )
        return s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
                "ResponseContentType": XLSX_CONTENT_TYPE,
                "ResponseCacheControl": "private, no-store",
            },
            ExpiresIn=EXPORT_URL_EXPIRES_SECONDS,
        )

    return LedgerApi(
        LedgerService(repository),
        verifier,
        image_deleter=delete_image,
        image_url_signer=sign_image_url,
        export_uploader=upload_export,
    )


XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str((response.get("Error") or {}).get("Code") or "")
    return code in {"404", "NoSuchKey", "NotFound"}


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(str(uri or "").strip())
    if parsed.scheme != "s3":
        return "", ""
    return str(parsed.netloc or "").strip(), str(parsed.path or "").lstrip("/")


def _optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from core.enums import FieldName
from core.models import Candidate, ExtractionResult
from inbox.models import ConversationSession
from inbox.repository_interface import InboxRepositoryProtocol

try:
    import boto3  # type: ignore
    from boto3.dynamodb.conditions import Key  # type: ignore
    from boto3.dynamodb.types import TypeSerializer  # type: ignore
    from botocore.exceptions import ClientError  # type: ignore
except Exception as exc:  # pragma: no cover - import guard for local envs
    boto3 = None
    Key = None
    TypeSerializer = None
    ClientError = Exception
    _BOTO3_IMPORT_ERROR = exc
else:
    _BOTO3_IMPORT_ERROR = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DynamoInboxRepository(InboxRepositoryProtocol):
    SESSION_USER_UPDATED_INDEX = "line_user_id_updated_at_index"
    AGGREGATE_RECEIPT_INDEX = "receipt_id_index"

    def __init__(
        self,
        *,
        region_name: str | None = None,
        table_prefix: str = "medstackocr",
        event_table_name: str | None = None,
        receipts_table_name: str | None = None,
        receipt_fields_table_name: str | None = None,
        sessions_table_name: str | None = None,
        aggregate_table_name: str | None = None,
        family_registry_table_name: str | None = None,
        learning_table_name: str | None = None,
        usage_guard_table_name: str | None = None,
        event_ttl_days: int = 7,
        dynamodb_resource: Any | None = None,
    ) -> None:
        if boto3 is None:
            raise RuntimeError(f"boto3 is required for DynamoInboxRepository: {_BOTO3_IMPORT_ERROR}")

        normalized_prefix = (table_prefix or "medstackocr").strip()
        self.event_ttl_days = max(1, int(event_ttl_days))
        self._ddb = dynamodb_resource or boto3.resource("dynamodb", region_name=region_name)
        self._event_table = self._ddb.Table(event_table_name or f"{normalized_prefix}-line-event-dedupe")
        self._receipts_table = self._ddb.Table(receipts_table_name or f"{normalized_prefix}-receipts")
        self._fields_table = self._ddb.Table(receipt_fields_table_name or f"{normalized_prefix}-receipt-fields")
        self._sessions_table = self._ddb.Table(sessions_table_name or f"{normalized_prefix}-sessions")
        self._aggregate_table = self._ddb.Table(aggregate_table_name or f"{normalized_prefix}-aggregate-entries")
        self._family_table = self._ddb.Table(family_registry_table_name or f"{normalized_prefix}-family-registry")
        self._learning_table = self._ddb.Table(learning_table_name or f"{normalized_prefix}-learning-rules")
        self._usage_guard_table = self._ddb.Table(usage_guard_table_name or f"{normalized_prefix}-ocr-usage-guard")
        self._ddb_serializer = TypeSerializer() if TypeSerializer is not None else None

    def mark_event_processed(self, event_id: str) -> bool:
        key = (event_id or "").strip()
        if not key:
            return False
        now = datetime.now(timezone.utc)
        expires = int((now + timedelta(days=self.event_ttl_days)).timestamp())
        try:
            self._event_table.put_item(
                Item={
                    "event_id": key,
                    "received_at": now.isoformat(),
                    "expires_at_epoch": expires,
                },
                ConditionExpression="attribute_not_exists(event_id)",
            )
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code == "ConditionalCheckFailedException":
                return False
            raise

    def save_receipt_result(
        self,
        receipt_id: str,
        line_user_id: str,
        line_message_id: str,
        image_path: str,
        image_sha256: str,
        result: ExtractionResult,
        processing_error: str | None = None,
    ) -> None:
        now = _utc_now()
        extracted_fields = _extracted_fields_from_result(result)
        duplicate_key = _build_duplicate_key(extracted_fields)
        self._receipts_table.put_item(
            Item={
                "receipt_id": receipt_id,
                "line_user_id": line_user_id,
                "line_message_id": line_message_id,
                "image_path": image_path,
                "image_sha256": image_sha256,
                "duplicate_key": duplicate_key,
                "document_id": result.document_id,
                "decision_status": result.decision.status.value,
                "decision_confidence": _as_decimal(result.decision.confidence),
                "processing_error": processing_error,
                "created_at": now,
                "updated_at": now,
            }
        )

        existing_rows = self._fields_table.query(
            KeyConditionExpression=Key("receipt_id").eq(receipt_id),
            ProjectionExpression="receipt_id, field_name",
        ).get("Items", [])

        with self._fields_table.batch_writer() as batch:
            for row in existing_rows:
                batch.delete_item(
                    Key={
                        "receipt_id": row["receipt_id"],
                        "field_name": row["field_name"],
                    }
                )
            for field_name, candidate in result.fields.items():
                if candidate is None:
                    continue
                batch.put_item(
                    Item={
                        "receipt_id": receipt_id,
                        "field_name": field_name,
                        "value_raw": _to_text(candidate.value_raw),
                        "value_normalized": _to_text(candidate.value_normalized),
                        "score": _as_decimal(candidate.score),
                        "ocr_confidence": _as_decimal(candidate.ocr_confidence),
                        "reasons_json": json.dumps(candidate.reasons, ensure_ascii=False),
                        "source": candidate.source,
                    }
                )

    def get_receipt_fields(self, receipt_id: str) -> dict[str, Any]:
        rows = self._fields_table.query(
            KeyConditionExpression=Key("receipt_id").eq(receipt_id),
        ).get("Items", [])
        result: dict[str, Any] = {}
        for row in rows:
            field_name = str(row.get("field_name", ""))
            value = row.get("value_normalized")
            if value in (None, ""):
                value = row.get("value_raw")
            if field_name == FieldName.PAYMENT_AMOUNT:
                parsed = _to_int(value)
                result[field_name] = parsed if parsed is not None else value
            else:
                result[field_name] = value
        return result

    def update_field_value(self, receipt_id: str, field_name: str, value: Any, source: str = "line_user") -> None:
        existing = self._fields_table.get_item(
            Key={"receipt_id": receipt_id, "field_name": field_name}
        ).get("Item")

        now_value = _to_text(value)
        if existing is None:
            self._fields_table.put_item(
                Item={
                    "receipt_id": receipt_id,
                    "field_name": field_name,
                    "value_raw": now_value,
                    "value_normalized": now_value,
                    "score": Decimal("0"),
                    "ocr_confidence": Decimal("1"),
                    "reasons_json": json.dumps(["updated_by_line_user"], ensure_ascii=False),
                    "source": source,
                }
            )
            return

        reasons = _merge_reasons(existing.get("reasons_json"), "updated_by_line_user")
        self._fields_table.put_item(
            Item={
                "receipt_id": receipt_id,
                "field_name": field_name,
                "value_raw": now_value,
                "value_normalized": now_value,
                "score": existing.get("score", Decimal("0")),
                "ocr_confidence": existing.get("ocr_confidence", Decimal("0")),
                "reasons_json": reasons,
                "source": source,
            }
        )

    def upsert_session(
        self,
        line_user_id: str,
        receipt_id: str,
        state: str,
        payload: dict[str, Any],
        expires_at: str,
        awaiting_field: str | None = None,
        session_id: str | None = None,
    ) -> str:
        sid = session_id or str(uuid4())
        now = _utc_now()
        self._sessions_table.put_item(
            Item={
                "session_id": sid,
                "line_user_id": line_user_id,
                "receipt_id": receipt_id,
                "state": state,
                "awaiting_field": awaiting_field,
                "payload_json": json.dumps(payload, ensure_ascii=False),
                "expires_at": expires_at,
                "expires_at_epoch": _iso_to_epoch(expires_at),
                "created_at": now,
                "updated_at": now,
            }
        )
        return sid

    def get_active_session(self, line_user_id: str) -> ConversationSession | None:
        now = _utc_now()
        rows = self._sessions_table.query(
            IndexName=self.SESSION_USER_UPDATED_INDEX,
            KeyConditionExpression=Key("line_user_id").eq(line_user_id),
            ScanIndexForward=False,
            Limit=5,
        ).get("Items", [])
        for row in rows:
            expires_at = str(row.get("expires_at", ""))
            if expires_at and expires_at <= now:
                continue
            raw_payload = _load_json(row.get("payload_json"))
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            return ConversationSession(
                session_id=str(row["session_id"]),
                line_user_id=str(row["line_user_id"]),
                receipt_id=str(row["receipt_id"]),
                state=str(row["state"]),
                awaiting_field=row.get("awaiting_field"),
                payload=payload,
                expires_at=expires_at,
                created_at=str(row.get("created_at", "")),
                updated_at=str(row.get("updated_at", "")),
            )
        return None

    def delete_session(self, session_id: str) -> None:
        self._sessions_table.delete_item(Key={"session_id": session_id})

    def upsert_aggregate_entry(
        self,
        receipt_id: str,
        line_user_id: str,
        fields: dict[str, Any],
        status: str,
    ) -> None:
        amount = _to_int(fields.get(FieldName.PAYMENT_AMOUNT))
        provider = _to_text(fields.get(FieldName.PAYER_FACILITY_NAME))
        if provider in (None, ""):
            provider = _to_text(fields.get(FieldName.PRESCRIBING_FACILITY_NAME))
        family_name = _to_text(fields.get(FieldName.FAMILY_MEMBER_NAME))
        service_date = _to_date_text(fields.get(FieldName.PAYMENT_DATE))
        now = _utc_now()

        existing = self._find_aggregate_by_receipt(receipt_id)
        if existing:
            partition_key = str(existing["line_user_id"])
            sort_key = str(existing["service_date_receipt"])
            created_at = str(existing.get("created_at", now))
        else:
            partition_key = line_user_id
            date_prefix = service_date or now[:10]
            sort_key = f"{date_prefix}#{receipt_id}"
            created_at = now

        self._aggregate_table.put_item(
            Item={
                "line_user_id": partition_key,
                "service_date_receipt": sort_key,
                "receipt_id": receipt_id,
                "service_date": service_date,
                "provider_name": provider,
                "amount_yen": amount,
                "family_member_name": family_name,
                "status": status,
                "created_at": created_at,
                "updated_at": now,
            }
        )

    def set_aggregate_status(self, receipt_id: str, status: str) -> None:
        existing = self._find_aggregate_by_receipt(receipt_id)
        if not existing:
            return
        self._aggregate_table.update_item(
            Key={
                "line_user_id": existing["line_user_id"],
                "service_date_receipt": existing["service_date_receipt"],
            },
            UpdateExpression="SET #status = :status, updated_at = :updated_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": status, ":updated_at": _utc_now()},
        )

    def get_year_summary(self, line_user_id: str, year: int) -> tuple[int, int]:
        rows = self._query_aggregate_entries(line_user_id=line_user_id)
        year_prefix = f"{year:04d}-"
        rows = [
            item
            for item in rows
            if str(_summary_date_from_values(item.get("service_date"), item.get("created_at")) or "").startswith(year_prefix)
        ]
        return _summarize(rows, statuses={"tentative", "confirmed"})

    def get_month_summary(self, line_user_id: str, year: int, month: int) -> tuple[int, int]:
        rows = self._query_aggregate_entries(line_user_id=line_user_id)
        month_prefix = f"{year:04d}-{month:02d}-"
        rows = [
            item
            for item in rows
            if str(_summary_date_from_values(item.get("service_date"), item.get("created_at")) or "").startswith(month_prefix)
        ]
        return _summarize(rows, statuses={"tentative", "confirmed"})

    def get_pending_count(self, line_user_id: str) -> int:
        rows = self._query_aggregate_entries(line_user_id=line_user_id)
        return sum(1 for row in rows if str(row.get("status", "")) in {"tentative", "hold"})

    def ensure_family_registration_started(self, line_user_id: str) -> bool:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return False
        now = _utc_now()
        profile_key = {"line_user_id": user_id, "record_type": "PROFILE"}
        existing = self._family_table.get_item(Key=profile_key).get("Item")
        if existing is None:
            try:
                self._family_table.put_item(
                    Item={
                        "line_user_id": user_id,
                        "record_type": "PROFILE",
                        "status": "in_progress",
                        "created_at": now,
                        "updated_at": now,
                        "completed_at": None,
                    },
                    ConditionExpression="attribute_not_exists(line_user_id) AND attribute_not_exists(record_type)",
                )
                return True
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code != "ConditionalCheckFailedException":
                    raise
                return False

        status = str(existing.get("status", "") or "")
        if status != "completed":
            self._family_table.update_item(
                Key=profile_key,
                UpdateExpression="SET #status = :status, updated_at = :updated_at",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":status": "in_progress", ":updated_at": now},
            )
        return False

    def is_family_registration_completed(self, line_user_id: str) -> bool:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return False
        row = self._family_table.get_item(
            Key={"line_user_id": user_id, "record_type": "PROFILE"},
            ProjectionExpression="#status",
            ExpressionAttributeNames={"#status": "status"},
        ).get("Item")
        if row is None:
            return False
        return str(row.get("status", "") or "") == "completed"

    def complete_family_registration(self, line_user_id: str) -> None:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return
        now = _utc_now()
        profile_key = {"line_user_id": user_id, "record_type": "PROFILE"}
        existing = self._family_table.get_item(Key=profile_key).get("Item")
        if existing is None:
            self._family_table.put_item(
                Item={
                    "line_user_id": user_id,
                    "record_type": "PROFILE",
                    "status": "completed",
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": now,
                }
            )
            return
        self._family_table.update_item(
            Key=profile_key,
            UpdateExpression="SET #status = :status, updated_at = :updated_at, completed_at = :completed_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "completed",
                ":updated_at": now,
                ":completed_at": now,
            },
        )

    def upsert_family_member(self, line_user_id: str, canonical_name: str, aliases: list[str]) -> str:
        user_id = str(line_user_id or "").strip()
        canonical = _normalize_family_name(canonical_name)
        if not user_id or not canonical:
            return ""

        sort_key = _family_member_record_type(canonical)
        now = _utc_now()
        key = {"line_user_id": user_id, "record_type": sort_key}
        existing = self._family_table.get_item(Key=key).get("Item")

        merged_aliases = _normalize_aliases([canonical, *aliases])
        if existing is None:
            self._family_table.put_item(
                Item={
                    "line_user_id": user_id,
                    "record_type": sort_key,
                    "canonical_name": canonical,
                    "aliases": merged_aliases,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            return sort_key

        existing_aliases = existing.get("aliases")
        inputs: list[Any] = [canonical]
        if isinstance(existing_aliases, list):
            inputs.extend(existing_aliases)
        inputs.extend(aliases)
        combined = _normalize_aliases(inputs)
        self._family_table.put_item(
            Item={
                "line_user_id": user_id,
                "record_type": sort_key,
                "canonical_name": canonical,
                "aliases": combined,
                "created_at": existing.get("created_at", now),
                "updated_at": now,
            }
        )
        return sort_key

    def list_family_members(self, line_user_id: str) -> list[dict[str, Any]]:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return []
        rows: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("line_user_id").eq(user_id)
                & Key("record_type").begins_with("MEMBER#")
            ),
        }
        while True:
            response = self._family_table.query(**kwargs)
            rows.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        members: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda item: str(item.get("created_at", ""))):
            canonical_name = _normalize_family_name(row.get("canonical_name"))
            aliases_raw = row.get("aliases")
            aliases = _normalize_aliases(aliases_raw if isinstance(aliases_raw, list) else [])
            aliases = [alias for alias in aliases if alias != canonical_name]
            if not canonical_name:
                continue
            members.append(
                {
                    "canonical_name": canonical_name,
                    "aliases": aliases,
                }
            )
        return members

    def purge_user_data(self, line_user_id: str) -> list[str]:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return []

        receipts = self._query_receipts_by_user(user_id)
        receipt_ids = [str(item.get("receipt_id", "")).strip() for item in receipts if str(item.get("receipt_id", "")).strip()]
        image_paths = [
            str(item.get("image_path", "")).strip()
            for item in receipts
            if str(item.get("image_path", "")).strip()
        ]

        for receipt_id in receipt_ids:
            self._delete_receipt_fields(receipt_id)

        if receipt_ids:
            with self._receipts_table.batch_writer() as batch:
                for receipt_id in receipt_ids:
                    batch.delete_item(Key={"receipt_id": receipt_id})

        sessions = self._query_sessions_by_user(user_id)
        if sessions:
            with self._sessions_table.batch_writer() as batch:
                for session in sessions:
                    session_id = str(session.get("session_id", "")).strip()
                    if not session_id:
                        continue
                    batch.delete_item(Key={"session_id": session_id})

        aggregates = self._query_aggregate_entries(line_user_id=user_id)
        if aggregates:
            with self._aggregate_table.batch_writer() as batch:
                for item in aggregates:
                    sort_key = str(item.get("service_date_receipt", "")).strip()
                    if not sort_key:
                        continue
                    batch.delete_item(
                        Key={
                            "line_user_id": user_id,
                            "service_date_receipt": sort_key,
                        }
                    )

        family_records = self._query_family_records_by_user(user_id)
        if family_records:
            with self._family_table.batch_writer() as batch:
                for item in family_records:
                    record_type = str(item.get("record_type", "")).strip()
                    if not record_type:
                        continue
                    batch.delete_item(
                        Key={
                            "line_user_id": user_id,
                            "record_type": record_type,
                        }
                    )

        learning_rules = self._query_learning_rules_by_user(user_id)
        if learning_rules:
            with self._learning_table.batch_writer() as batch:
                for item in learning_rules:
                    rule_key = str(item.get("rule_key", "")).strip()
                    if not rule_key:
                        continue
                    batch.delete_item(
                        Key={
                            "line_user_id": user_id,
                            "rule_key": rule_key,
                        }
                    )

        usage_records = self._query_ocr_guard_records_by_scope(_ocr_guard_scope_user(user_id))
        if usage_records:
            with self._usage_guard_table.batch_writer() as batch:
                for item in usage_records:
                    scope_key = str(item.get("scope_key", "")).strip()
                    window_key = str(item.get("window_key", "")).strip()
                    if not scope_key or not window_key:
                        continue
                    batch.delete_item(
                        Key={
                            "scope_key": scope_key,
                            "window_key": window_key,
                        }
                    )

        return sorted(set(image_paths))

    def record_field_correction(
        self,
        line_user_id: str,
        field_name: str,
        context_key: str,
        corrected_value: Any,
    ) -> None:
        user_id = str(line_user_id or "").strip()
        normalized_field = str(field_name or "").strip()
        normalized_context = _normalize_context_key(context_key)
        normalized_value = _normalize_learning_value(corrected_value)
        if not user_id or not normalized_field or not normalized_context or normalized_value is None:
            return
        rule_key = _learning_rule_key(normalized_field, normalized_context, normalized_value)
        now = _utc_now()
        try:
            self._increment_learning_rule_count(
                line_user_id=user_id,
                rule_key=rule_key,
                field_name=normalized_field,
                context_key=normalized_context,
                corrected_value=normalized_value,
                now=now,
            )
        except ClientError as exc:
            if not _is_add_operand_type_error(exc):
                raise
            self._normalize_learning_rule_count(
                line_user_id=user_id,
                rule_key=rule_key,
                updated_at=now,
            )
            self._increment_learning_rule_count(
                line_user_id=user_id,
                rule_key=rule_key,
                field_name=normalized_field,
                context_key=normalized_context,
                corrected_value=normalized_value,
                now=now,
            )

    def get_field_correction_hint(
        self,
        line_user_id: str,
        field_name: str,
        context_key: str,
        min_count: int = 2,
    ) -> Any | None:
        user_id = str(line_user_id or "").strip()
        normalized_field = str(field_name or "").strip()
        normalized_context = _normalize_context_key(context_key)
        threshold = max(1, int(min_count))
        if not user_id or not normalized_field or not normalized_context:
            return None
        rows = self._query_learning_rules_by_prefix(
            line_user_id=user_id,
            prefix=f"{normalized_field}#{normalized_context}#",
        )
        if not rows:
            return None
        best = sorted(
            rows,
            key=lambda item: (int(item.get("count", 0) or 0), str(item.get("updated_at", ""))),
            reverse=True,
        )[0]
        if int(best.get("count", 0) or 0) < threshold:
            return None
        value = str(best.get("corrected_value", "") or "")
        if normalized_field == FieldName.PAYMENT_AMOUNT:
            parsed = _to_int(value)
            return parsed if parsed is not None else value
        return value

    def find_potential_duplicates(
        self,
        line_user_id: str,
        image_sha256: str,
        duplicate_key: str | None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        user_id = str(line_user_id or "").strip()
        image_hash = str(image_sha256 or "").strip()
        normalized_key = str(duplicate_key or "").strip()
        max_items = max(1, int(limit))
        if not user_id:
            return []

        rows = self._query_receipts_by_user(
            user_id,
            projection="receipt_id, image_sha256, duplicate_key, created_at",
        )
        duplicates: list[dict[str, Any]] = []
        for item in rows:
            reasons: list[str] = []
            if image_hash and str(item.get("image_sha256", "") or "") == image_hash:
                reasons.append("image_sha256")
            if normalized_key and str(item.get("duplicate_key", "") or "") == normalized_key:
                reasons.append("fields")
            if not reasons:
                continue
            receipt_id = str(item.get("receipt_id", "") or "").strip()
            if not receipt_id:
                continue
            duplicates.append(
                {
                    "receipt_id": receipt_id,
                    "created_at": str(item.get("created_at", "") or ""),
                    "reasons": reasons,
                }
            )
            if len(duplicates) >= max_items:
                break
        return duplicates

    def get_latest_receipt_id(self, line_user_id: str) -> str | None:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return None
        rows = self._query_receipts_by_user(
            user_id,
            projection="receipt_id",
            scan_index_forward=False,
            limit=1,
        )
        if not rows:
            return None
        receipt_id = str(rows[0].get("receipt_id", "") or "").strip()
        return receipt_id or None

    def consume_ocr_quota(
        self,
        *,
        line_user_id: str,
        now_utc: datetime,
        user_per_minute_limit: int,
        user_per_day_limit: int,
        global_per_day_limit: int,
    ) -> tuple[bool, str | None]:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return False, "user_minute"

        user_minute_limit = max(1, int(user_per_minute_limit))
        user_day_limit = max(1, int(user_per_day_limit))
        global_day_limit = max(1, int(global_per_day_limit))
        now = now_utc.astimezone(timezone.utc)
        now_epoch = int(now.timestamp())
        now_text = now.isoformat()
        minute_token = now.strftime("%Y%m%d%H%M")
        day_token = now.strftime("%Y%m%d")

        buckets = [
            {
                "scope_key": _ocr_guard_scope_user(user_id),
                "window_key": f"MIN#{minute_token}",
                "limit": user_minute_limit,
                "reason": "user_minute",
                "expires_at_epoch": now_epoch + 120,
            },
            {
                "scope_key": _ocr_guard_scope_user(user_id),
                "window_key": f"DAY#{day_token}",
                "limit": user_day_limit,
                "reason": "user_day",
                "expires_at_epoch": now_epoch + 172800,
            },
            {
                "scope_key": _ocr_guard_scope_global(),
                "window_key": f"DAY#{day_token}",
                "limit": global_day_limit,
                "reason": "global_day",
                "expires_at_epoch": now_epoch + 172800,
            },
        ]

        transact_items: list[dict[str, Any]] = []
        for bucket in buckets:
            transact_items.append(
                {
                    "Update": {
                        "TableName": self._usage_guard_table.name,
                        "Key": {
                            "scope_key": bucket["scope_key"],
                            "window_key": bucket["window_key"],
                        },
                        "UpdateExpression": (
                            "ADD #count :incr "
                            "SET expires_at_epoch = if_not_exists(expires_at_epoch, :expires), updated_at = :updated_at"
                        ),
                        "ConditionExpression": "attribute_not_exists(#count) OR #count < :limit",
                        "ExpressionAttributeNames": {"#count": "count"},
                        "ExpressionAttributeValues": {
                            ":incr": 1,
                            ":limit": int(bucket["limit"]),
                            ":expires": int(bucket["expires_at_epoch"]),
                            ":updated_at": now_text,
                        },
                    }
                }
            )

        client = self._ddb.meta.client
        try:
            client.transact_write_items(TransactItems=transact_items)
            return True, None
        except ClientError as exc:
            if _is_add_operand_type_error(exc):
                self._normalize_ocr_guard_count_types(buckets=buckets, updated_at=now_text)
                try:
                    client.transact_write_items(TransactItems=transact_items)
                    return True, None
                except ClientError as retry_exc:
                    code = str(retry_exc.response.get("Error", {}).get("Code", ""))
                    if code != "TransactionCanceledException":
                        raise
                    return False, self._detect_ocr_quota_reject_reason(buckets)
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code != "TransactionCanceledException":
                raise
            return False, self._detect_ocr_quota_reject_reason(buckets)

    def delete_receipt(self, line_user_id: str, receipt_id: str) -> str | None:
        user_id = str(line_user_id or "").strip()
        rid = str(receipt_id or "").strip()
        if not user_id or not rid:
            return None

        row = self._receipts_table.get_item(Key={"receipt_id": rid}).get("Item")
        if row is None:
            return None
        if str(row.get("line_user_id", "") or "") != user_id:
            return None

        image_path = str(row.get("image_path", "") or "").strip() or None
        self._delete_receipt_fields(rid)
        self._receipts_table.delete_item(Key={"receipt_id": rid})

        aggregate = self._find_aggregate_by_receipt(rid)
        if aggregate is not None:
            self._aggregate_table.delete_item(
                Key={
                    "line_user_id": aggregate["line_user_id"],
                    "service_date_receipt": aggregate["service_date_receipt"],
                }
            )

        sessions = self._query_sessions_by_user(user_id)
        for session in sessions:
            if str(session.get("receipt_id", "") or "") != rid:
                continue
            session_id = str(session.get("session_id", "") or "").strip()
            if not session_id:
                continue
            self._sessions_table.delete_item(Key={"session_id": session_id})

        return image_path

    def _query_aggregate_entries(self, *, line_user_id: str, prefix: str | None = None) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("line_user_id").eq(line_user_id),
        }
        if prefix:
            kwargs["KeyConditionExpression"] = (
                Key("line_user_id").eq(line_user_id)
                & Key("service_date_receipt").begins_with(prefix)
            )
        items: list[dict[str, Any]] = []
        while True:
            response = self._aggregate_table.query(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return items

    def _query_receipts_by_user(
        self,
        line_user_id: str,
        projection: str = "receipt_id, image_path",
        scan_index_forward: bool = True,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "IndexName": "gsi_user_created",
            "KeyConditionExpression": Key("line_user_id").eq(line_user_id),
            "ProjectionExpression": projection,
            "ScanIndexForward": scan_index_forward,
        }
        if limit is not None and limit > 0:
            kwargs["Limit"] = int(limit)
        items: list[dict[str, Any]] = []
        while True:
            response = self._receipts_table.query(**kwargs)
            items.extend(response.get("Items", []))
            if limit is not None and limit > 0 and len(items) >= limit:
                return items[:limit]
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return items

    def _query_sessions_by_user(self, line_user_id: str) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "IndexName": self.SESSION_USER_UPDATED_INDEX,
            "KeyConditionExpression": Key("line_user_id").eq(line_user_id),
            "ProjectionExpression": "session_id, receipt_id",
        }
        items: list[dict[str, Any]] = []
        while True:
            response = self._sessions_table.query(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return items

    def _query_family_records_by_user(self, line_user_id: str) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("line_user_id").eq(line_user_id),
            "ProjectionExpression": "line_user_id, record_type",
        }
        items: list[dict[str, Any]] = []
        while True:
            response = self._family_table.query(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return items

    def _query_learning_rules_by_user(self, line_user_id: str) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("line_user_id").eq(line_user_id),
            "ProjectionExpression": "line_user_id, rule_key",
        }
        items: list[dict[str, Any]] = []
        while True:
            response = self._learning_table.query(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return items

    def _query_learning_rules_by_prefix(self, *, line_user_id: str, prefix: str) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("line_user_id").eq(line_user_id)
                & Key("rule_key").begins_with(prefix)
            ),
            "ProjectionExpression": "rule_key, corrected_value, #count, updated_at",
            "ExpressionAttributeNames": {"#count": "count"},
        }
        items: list[dict[str, Any]] = []
        while True:
            response = self._learning_table.query(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return items

    def _query_ocr_guard_records_by_scope(self, scope_key: str) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("scope_key").eq(scope_key),
            "ProjectionExpression": "scope_key, window_key",
        }
        items: list[dict[str, Any]] = []
        while True:
            response = self._usage_guard_table.query(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return items

    def _increment_learning_rule_count(
        self,
        *,
        line_user_id: str,
        rule_key: str,
        field_name: str,
        context_key: str,
        corrected_value: Any,
        now: str,
    ) -> None:
        self._learning_table.update_item(
            Key={"line_user_id": line_user_id, "rule_key": rule_key},
            UpdateExpression=(
                "SET #field_name = :field_name, #context_key = :context_key, corrected_value = :corrected_value, "
                "updated_at = :updated_at, created_at = if_not_exists(created_at, :created_at) "
                "ADD #count :incr"
            ),
            ExpressionAttributeNames={
                "#count": "count",
                "#field_name": "field_name",
                "#context_key": "context_key",
            },
            ExpressionAttributeValues={
                ":field_name": field_name,
                ":context_key": context_key,
                ":corrected_value": corrected_value,
                ":updated_at": now,
                ":created_at": now,
                ":incr": 1,
            },
        )

    def _normalize_learning_rule_count(
        self,
        *,
        line_user_id: str,
        rule_key: str,
        updated_at: str,
    ) -> None:
        item = self._learning_table.get_item(
            Key={"line_user_id": line_user_id, "rule_key": rule_key},
            ConsistentRead=True,
        ).get("Item")
        if not isinstance(item, dict):
            return
        raw_count = item.get("count")
        if raw_count is None or _is_add_compatible_count(raw_count):
            return
        normalized_count = _coerce_add_count(raw_count)
        self._learning_table.update_item(
            Key={"line_user_id": line_user_id, "rule_key": rule_key},
            UpdateExpression="SET #count = :count, updated_at = :updated_at",
            ExpressionAttributeNames={"#count": "count"},
            ExpressionAttributeValues={
                ":count": normalized_count,
                ":updated_at": updated_at,
            },
        )

    def _normalize_ocr_guard_count_types(
        self,
        *,
        buckets: list[dict[str, Any]],
        updated_at: str,
    ) -> None:
        for bucket in buckets:
            key = {
                "scope_key": bucket["scope_key"],
                "window_key": bucket["window_key"],
            }
            item = self._usage_guard_table.get_item(
                Key=key,
                ConsistentRead=True,
            ).get("Item")
            if not isinstance(item, dict):
                continue
            raw_count = item.get("count")
            if raw_count is None or _is_add_compatible_count(raw_count):
                continue
            normalized_count = _coerce_add_count(raw_count)
            self._usage_guard_table.update_item(
                Key=key,
                UpdateExpression="SET #count = :count, updated_at = :updated_at",
                ExpressionAttributeNames={"#count": "count"},
                ExpressionAttributeValues={
                    ":count": normalized_count,
                    ":updated_at": updated_at,
                },
            )

    def _detect_ocr_quota_reject_reason(self, buckets: list[dict[str, Any]]) -> str:
        for bucket in buckets:
            item = self._usage_guard_table.get_item(
                Key={
                    "scope_key": bucket["scope_key"],
                    "window_key": bucket["window_key"],
                },
                ConsistentRead=True,
            ).get("Item")
            current = int(item.get("count", 0) or 0) if isinstance(item, dict) else 0
            if current >= int(bucket["limit"]):
                return str(bucket["reason"])
        return "global_day"

    def _to_ddb_key(self, value: dict[str, Any]) -> dict[str, Any]:
        if self._ddb_serializer is None:
            raise RuntimeError("TypeSerializer is unavailable for DynamoDB transaction")
        return {key: self._ddb_serializer.serialize(val) for key, val in value.items()}

    def _delete_receipt_fields(self, receipt_id: str) -> None:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("receipt_id").eq(receipt_id),
            "ProjectionExpression": "receipt_id, field_name",
        }
        while True:
            response = self._fields_table.query(**kwargs)
            rows = response.get("Items", [])
            if rows:
                with self._fields_table.batch_writer() as batch:
                    for row in rows:
                        batch.delete_item(
                            Key={
                                "receipt_id": str(row.get("receipt_id", "")).strip(),
                                "field_name": str(row.get("field_name", "")).strip(),
                            }
                        )
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _find_aggregate_by_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        response = self._aggregate_table.query(
            IndexName=self.AGGREGATE_RECEIPT_INDEX,
            KeyConditionExpression=Key("receipt_id").eq(receipt_id),
            Limit=1,
        )
        items = response.get("Items", [])
        return items[0] if items else None


def _summarize(rows: list[dict[str, Any]], statuses: set[str]) -> tuple[int, int]:
    total = 0
    count = 0
    for row in rows:
        if str(row.get("status", "")) not in statuses:
            continue
        amount = _to_int(row.get("amount_yen")) or 0
        total += amount
        count += 1
    return total, count


def _is_add_operand_type_error(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    if not isinstance(error, dict):
        return False
    code = str(error.get("Code", ""))
    if code != "ValidationException":
        return False
    message = str(error.get("Message", ""))
    return "operator: ADD" in message and "ALLOWED_FOR_ADD_OPERAND" in message


def _is_add_compatible_count(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float, Decimal))


def _coerce_add_count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(text)
        except ValueError:
            try:
                return int(Decimal(text))
            except Exception:
                return 0
    if isinstance(value, dict):
        for key in ("N", "n", "value", "count", "S", "s"):
            if key not in value:
                continue
            return _coerce_add_count(value.get(key))
    return 0


def _as_decimal(value: float) -> Decimal:
    return Decimal(str(float(value)))


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        if number.is_integer():
            return str(int(number))
        return str(number)
    if isinstance(value, Candidate):
        return _to_text(value.value_normalized)
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _merge_reasons(reasons_json: str | None, new_reason: str) -> str:
    reasons = _load_json(reasons_json) if reasons_json else []
    if not isinstance(reasons, list):
        reasons = []
    if new_reason not in reasons:
        reasons.append(new_reason)
    return json.dumps(reasons, ensure_ascii=False)


def _load_json(text: str | None) -> Any:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
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
    text = (str(value).strip() if value is not None else "")
    if not text:
        return None
    candidates = [text]
    if len(text) >= 10:
        candidates.append(text[:10])
    for candidate in candidates:
        full_match = _FULL_DATE_RE.match(candidate)
        if full_match is not None:
            try:
                parsed = datetime(
                    year=int(full_match.group("year")),
                    month=int(full_match.group("month")),
                    day=int(full_match.group("day")),
                )
            except Exception:
                continue
            return parsed.strftime("%Y-%m-%d")
        short_match = _MONTH_DAY_RE.match(candidate)
        if short_match is not None:
            month = int(short_match.group("month"))
            day = int(short_match.group("day"))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{month:02d}-{day:02d}"
    return text


def _summary_date_from_values(service_date: Any, created_at: Any) -> str | None:
    normalized = _to_date_text(service_date)
    if normalized and _CANONICAL_FULL_DATE_RE.match(normalized):
        return normalized

    created_text = str(created_at or "").strip()
    created_date = _to_date_text(created_text[:10]) if len(created_text) >= 10 else _to_date_text(created_text)
    if normalized and _CANONICAL_MONTH_DAY_RE.match(normalized):
        if created_date and _CANONICAL_FULL_DATE_RE.match(created_date):
            return f"{created_date[:4]}-{normalized}"
        return None
    if created_date and _CANONICAL_FULL_DATE_RE.match(created_date):
        return created_date
    return None


_FULL_DATE_RE = re.compile(r"^(?P<year>\d{4})\s*[\/\-.年]\s*(?P<month>\d{1,2})\s*[\/\-.月]\s*(?P<day>\d{1,2})\s*日?$")
_MONTH_DAY_RE = re.compile(r"^(?P<month>\d{1,2})\s*[\/\-.月]\s*(?P<day>\d{1,2})\s*日?$")
_CANONICAL_FULL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CANONICAL_MONTH_DAY_RE = re.compile(r"^\d{2}-\d{2}$")


def _iso_to_epoch(text: str) -> int:
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _normalize_family_name(value: Any) -> str:
    text = str(value or "").replace("\u3000", " ").strip()
    return " ".join(part for part in text.split(" ") if part)


def _normalize_aliases(values: list[Any]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = _normalize_family_name(value)
        if not alias:
            continue
        key = alias.lower()
        if key in seen:
            continue
        seen.add(key)
        aliases.append(alias)
    return aliases


def _family_member_record_type(canonical_name: str) -> str:
    normalized = _normalize_family_name(canonical_name).lower().encode("utf-8")
    digest = hashlib.sha256(normalized).hexdigest()[:32]
    return f"MEMBER#{digest}"


def _normalize_learning_value(value: Any) -> str | None:
    text = _to_text(value)
    if text is None:
        return None
    normalized = str(text).strip()
    return normalized or None


def _normalize_context_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _learning_rule_key(field_name: str, context_key: str, corrected_value: str) -> str:
    digest = hashlib.sha1(corrected_value.encode("utf-8")).hexdigest()
    return f"{field_name}#{context_key}#{digest}"


def _extracted_fields_from_result(result: ExtractionResult) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for field_name, candidate in result.fields.items():
        if candidate is None:
            continue
        value = candidate.value_normalized
        if value in (None, ""):
            value = candidate.value_raw
        output[field_name] = value
    return output


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
    compact = "".join(ch for ch in text if ch.isalnum() or ch in {"-", "_"})
    return compact


def _ocr_guard_scope_user(line_user_id: str) -> str:
    return f"USER#{line_user_id}"


def _ocr_guard_scope_global() -> str:
    return "GLOBAL"

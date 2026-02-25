from __future__ import annotations

from typing import Any

from inbox.dynamo_repository import DynamoInboxRepository
from inbox.repository import InboxRepository
from inbox.repository_interface import InboxRepositoryProtocol


def create_inbox_repository(config: dict[str, Any]) -> InboxRepositoryProtocol:
    inbox_conf = config.get("inbox", {})
    backend = str(inbox_conf.get("backend", "sqlite") or "sqlite").strip().lower()

    if backend == "dynamodb":
        ddb_conf = inbox_conf.get("dynamodb", {}) if isinstance(inbox_conf, dict) else {}
        tables = ddb_conf.get("tables", {}) if isinstance(ddb_conf, dict) else {}
        return DynamoInboxRepository(
            region_name=_as_optional_str(ddb_conf.get("region")),
            table_prefix=str(ddb_conf.get("table_prefix", "medstackocr")),
            event_table_name=_as_optional_str(tables.get("event_dedupe")),
            receipts_table_name=_as_optional_str(tables.get("receipts")),
            receipt_fields_table_name=_as_optional_str(tables.get("receipt_fields")),
            sessions_table_name=_as_optional_str(tables.get("sessions")),
            aggregate_table_name=_as_optional_str(tables.get("aggregate_entries")),
            family_registry_table_name=_as_optional_str(tables.get("family_registry")),
            learning_table_name=_as_optional_str(tables.get("learning_rules")),
            usage_guard_table_name=_as_optional_str(tables.get("ocr_usage_guard")),
            event_ttl_days=int(ddb_conf.get("event_ttl_days", 7)),
        )

    sqlite_path = str(inbox_conf.get("sqlite_path", "data/inbox/linebot.db"))
    return InboxRepository(sqlite_path=sqlite_path)


def _as_optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None

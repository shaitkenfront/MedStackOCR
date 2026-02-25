from __future__ import annotations

from typing import Any, Protocol

from core.models import ExtractionResult
from inbox.models import ConversationSession


class InboxRepositoryProtocol(Protocol):
    def mark_event_processed(self, event_id: str) -> bool: ...

    def save_receipt_result(
        self,
        receipt_id: str,
        line_user_id: str,
        line_message_id: str,
        image_path: str,
        image_sha256: str,
        result: ExtractionResult,
        processing_error: str | None = None,
    ) -> None: ...

    def get_receipt_fields(self, receipt_id: str) -> dict[str, Any]: ...

    def update_field_value(self, receipt_id: str, field_name: str, value: Any, source: str = "line_user") -> None: ...

    def upsert_session(
        self,
        line_user_id: str,
        receipt_id: str,
        state: str,
        payload: dict[str, Any],
        expires_at: str,
        awaiting_field: str | None = None,
        session_id: str | None = None,
    ) -> str: ...

    def get_active_session(self, line_user_id: str) -> ConversationSession | None: ...

    def delete_session(self, session_id: str) -> None: ...

    def upsert_aggregate_entry(
        self,
        receipt_id: str,
        line_user_id: str,
        fields: dict[str, Any],
        status: str,
    ) -> None: ...

    def set_aggregate_status(self, receipt_id: str, status: str) -> None: ...

    def get_year_summary(self, line_user_id: str, year: int) -> tuple[int, int]: ...

    def get_month_summary(self, line_user_id: str, year: int, month: int) -> tuple[int, int]: ...

    def get_pending_count(self, line_user_id: str) -> int: ...

    def ensure_family_registration_started(self, line_user_id: str) -> bool: ...

    def is_family_registration_completed(self, line_user_id: str) -> bool: ...

    def complete_family_registration(self, line_user_id: str) -> None: ...

    def upsert_family_member(self, line_user_id: str, canonical_name: str, aliases: list[str]) -> str: ...

    def list_family_members(self, line_user_id: str) -> list[dict[str, Any]]: ...

    def purge_user_data(self, line_user_id: str) -> list[str]: ...

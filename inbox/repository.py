from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from core.enums import FieldName
from core.models import Candidate, ExtractionResult
from inbox.models import ConversationSession


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InboxRepository:
    def __init__(self, sqlite_path: str) -> None:
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    line_user_id TEXT NOT NULL,
                    line_message_id TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    image_sha256 TEXT NOT NULL,
                    duplicate_key TEXT,
                    document_id TEXT,
                    decision_status TEXT NOT NULL,
                    decision_confidence REAL NOT NULL,
                    processing_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS receipt_fields (
                    receipt_id TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    value_raw TEXT,
                    value_normalized TEXT,
                    score REAL,
                    ocr_confidence REAL,
                    reasons_json TEXT,
                    source TEXT,
                    PRIMARY KEY (receipt_id, field_name)
                );

                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    session_id TEXT PRIMARY KEY,
                    line_user_id TEXT NOT NULL,
                    receipt_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    awaiting_field TEXT,
                    payload_json TEXT,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
                    ON conversation_sessions(line_user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS aggregate_entries (
                    entry_id TEXT PRIMARY KEY,
                    receipt_id TEXT UNIQUE NOT NULL,
                    line_user_id TEXT NOT NULL,
                    service_date TEXT,
                    provider_name TEXT,
                    amount_yen INTEGER,
                    family_member_name TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_aggregate_user_date
                    ON aggregate_entries(line_user_id, service_date);

                CREATE TABLE IF NOT EXISTS processed_line_events (
                    event_id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS family_registry_profiles (
                    line_user_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS family_registry_members (
                    line_user_id TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (line_user_id, member_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_family_registry_user_canonical
                    ON family_registry_members(line_user_id, canonical_name);

                CREATE TABLE IF NOT EXISTS correction_rules (
                    line_user_id TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    corrected_value TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (line_user_id, field_name, context_key, corrected_value)
                );
                CREATE INDEX IF NOT EXISTS idx_correction_rules_lookup
                    ON correction_rules(line_user_id, field_name, context_key, count DESC, updated_at DESC);

                CREATE TABLE IF NOT EXISTS ocr_usage_guard (
                    scope_key TEXT NOT NULL,
                    window_key TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    expires_at_epoch INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope_key, window_key)
                );
                CREATE INDEX IF NOT EXISTS idx_ocr_usage_guard_expires
                    ON ocr_usage_guard(expires_at_epoch);
                """
            )
            self._ensure_column_exists(conn, "receipts", "duplicate_key", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_receipts_user_hash ON receipts(line_user_id, image_sha256)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_receipts_user_duplicate_key ON receipts(line_user_id, duplicate_key)"
            )
            conn.commit()

    @staticmethod
    def _ensure_column_exists(conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column_name in existing:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    def mark_event_processed(self, event_id: str) -> bool:
        key = (event_id or "").strip()
        if not key:
            return False

        with self._connect() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO processed_line_events(event_id, received_at) VALUES(?, ?)", (key, _utc_now()))
            conn.commit()
            return cur.rowcount > 0

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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO receipts(
                    receipt_id, line_user_id, line_message_id, image_path, image_sha256, duplicate_key,
                    document_id, decision_status, decision_confidence, processing_error,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    line_user_id,
                    line_message_id,
                    image_path,
                    image_sha256,
                    duplicate_key,
                    result.document_id,
                    result.decision.status.value,
                    float(result.decision.confidence),
                    processing_error,
                    now,
                    now,
                ),
            )
            for field_name, candidate in result.fields.items():
                if candidate is None:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO receipt_fields(
                        receipt_id, field_name, value_raw, value_normalized,
                        score, ocr_confidence, reasons_json, source
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        field_name,
                        _to_text(candidate.value_raw),
                        _to_text(candidate.value_normalized),
                        float(candidate.score),
                        float(candidate.ocr_confidence),
                        json.dumps(candidate.reasons, ensure_ascii=False),
                        candidate.source,
                    ),
                )
            conn.commit()

    def get_receipt_fields(self, receipt_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT field_name, value_normalized, value_raw
                FROM receipt_fields
                WHERE receipt_id = ?
                """,
                (receipt_id,),
            ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            value = row["value_normalized"]
            if value in (None, ""):
                value = row["value_raw"]
            if row["field_name"] == FieldName.PAYMENT_AMOUNT:
                parsed = _to_int(value)
                result[row["field_name"]] = parsed if parsed is not None else value
            else:
                result[row["field_name"]] = value
        return result

    def update_field_value(self, receipt_id: str, field_name: str, value: Any, source: str = "line_user") -> None:
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT value_raw, value_normalized, score, ocr_confidence, reasons_json
                FROM receipt_fields
                WHERE receipt_id = ? AND field_name = ?
                """,
                (receipt_id, field_name),
            ).fetchone()
            now_value = _to_text(value)
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO receipt_fields(
                        receipt_id, field_name, value_raw, value_normalized, score, ocr_confidence, reasons_json, source
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        field_name,
                        now_value,
                        now_value,
                        0.0,
                        1.0,
                        json.dumps(["updated_by_line_user"], ensure_ascii=False),
                        source,
                    ),
                )
            else:
                reasons = _merge_reasons(existing["reasons_json"], "updated_by_line_user")
                conn.execute(
                    """
                    UPDATE receipt_fields
                    SET value_raw = ?, value_normalized = ?, source = ?, reasons_json = ?
                    WHERE receipt_id = ? AND field_name = ?
                    """,
                    (now_value, now_value, source, reasons, receipt_id, field_name),
                )
            conn.commit()

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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO conversation_sessions(
                    session_id, line_user_id, receipt_id, state, awaiting_field,
                    payload_json, expires_at, created_at, updated_at
                ) VALUES(
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    COALESCE((SELECT created_at FROM conversation_sessions WHERE session_id = ?), ?),
                    ?
                )
                """,
                (
                    sid,
                    line_user_id,
                    receipt_id,
                    state,
                    awaiting_field,
                    json.dumps(payload, ensure_ascii=False),
                    expires_at,
                    sid,
                    now,
                    now,
                ),
            )
            conn.commit()
        return sid

    def get_active_session(self, line_user_id: str) -> ConversationSession | None:
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, line_user_id, receipt_id, state, awaiting_field,
                       payload_json, expires_at, created_at, updated_at
                FROM conversation_sessions
                WHERE line_user_id = ? AND expires_at > ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (line_user_id, now),
            ).fetchone()
        if row is None:
            return None
        raw_payload = _load_json(row["payload_json"])
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        return ConversationSession(
            session_id=row["session_id"],
            line_user_id=row["line_user_id"],
            receipt_id=row["receipt_id"],
            state=row["state"],
            awaiting_field=row["awaiting_field"],
            payload=payload,
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def delete_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM conversation_sessions WHERE session_id = ?", (session_id,))
            conn.commit()

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

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT entry_id, created_at FROM aggregate_entries WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            entry_id = existing["entry_id"] if existing else str(uuid4())
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO aggregate_entries(
                    entry_id, receipt_id, line_user_id, service_date,
                    provider_name, amount_yen, family_member_name, status,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    receipt_id,
                    line_user_id,
                    service_date,
                    provider,
                    amount,
                    family_name,
                    status,
                    created_at,
                    now,
                ),
            )
            conn.commit()

    def set_aggregate_status(self, receipt_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE aggregate_entries SET status = ?, updated_at = ? WHERE receipt_id = ?",
                (status, _utc_now(), receipt_id),
            )
            conn.commit()

    def get_year_summary(self, line_user_id: str, year: int) -> tuple[int, int]:
        year_text = f"{year:04d}"
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(COALESCE(amount_yen, 0)), 0) AS total,
                       COUNT(*) AS count
                FROM aggregate_entries
                WHERE line_user_id = ?
                  AND status IN ('tentative', 'confirmed')
                  AND SUBSTR(COALESCE(service_date, created_at), 1, 4) = ?
                """,
                (line_user_id, year_text),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["total"] or 0), int(row["count"] or 0)

    def get_month_summary(self, line_user_id: str, year: int, month: int) -> tuple[int, int]:
        ym = f"{year:04d}-{month:02d}"
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(COALESCE(amount_yen, 0)), 0) AS total,
                       COUNT(*) AS count
                FROM aggregate_entries
                WHERE line_user_id = ?
                  AND status IN ('tentative', 'confirmed')
                  AND SUBSTR(COALESCE(service_date, created_at), 1, 7) = ?
                """,
                (line_user_id, ym),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["total"] or 0), int(row["count"] or 0)

    def get_pending_count(self, line_user_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM aggregate_entries
                WHERE line_user_id = ?
                  AND status IN ('tentative', 'hold')
                """,
                (line_user_id,),
            ).fetchone()
        return int((row["count"] if row else 0) or 0)

    def ensure_family_registration_started(self, line_user_id: str) -> bool:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return False
        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT status, created_at FROM family_registry_profiles WHERE line_user_id = ?",
                (user_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO family_registry_profiles(
                        line_user_id, status, created_at, updated_at, completed_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (user_id, "in_progress", now, now, None),
                )
                conn.commit()
                return True
            status = str(existing["status"] or "")
            if status != "completed":
                conn.execute(
                    """
                    UPDATE family_registry_profiles
                    SET status = ?, updated_at = ?
                    WHERE line_user_id = ?
                    """,
                    ("in_progress", now, user_id),
                )
                conn.commit()
        return False

    def is_family_registration_completed(self, line_user_id: str) -> bool:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM family_registry_profiles WHERE line_user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return False
        return str(row["status"] or "") == "completed"

    def complete_family_registration(self, line_user_id: str) -> None:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return
        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT line_user_id FROM family_registry_profiles WHERE line_user_id = ?",
                (user_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO family_registry_profiles(
                        line_user_id, status, created_at, updated_at, completed_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (user_id, "completed", now, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE family_registry_profiles
                    SET status = ?, updated_at = ?, completed_at = ?
                    WHERE line_user_id = ?
                    """,
                    ("completed", now, now, user_id),
                )
            conn.commit()

    def upsert_family_member(self, line_user_id: str, canonical_name: str, aliases: list[str]) -> str:
        user_id = str(line_user_id or "").strip()
        canonical = _normalize_family_name(canonical_name)
        if not user_id or not canonical:
            return ""

        merged_aliases = _normalize_aliases([canonical, *aliases])
        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT member_id, aliases_json, created_at
                FROM family_registry_members
                WHERE line_user_id = ? AND canonical_name = ?
                """,
                (user_id, canonical),
            ).fetchone()

            if existing is None:
                member_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO family_registry_members(
                        line_user_id, member_id, canonical_name, aliases_json, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        member_id,
                        canonical,
                        json.dumps(merged_aliases, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                conn.commit()
                return member_id

            existing_aliases = _load_json(existing["aliases_json"])
            merged_inputs: list[Any] = [canonical]
            if isinstance(existing_aliases, list):
                merged_inputs.extend(existing_aliases)
            merged_inputs.extend(aliases)
            combined = _normalize_aliases(merged_inputs)
            member_id = str(existing["member_id"])
            conn.execute(
                """
                UPDATE family_registry_members
                SET aliases_json = ?, updated_at = ?
                WHERE line_user_id = ? AND member_id = ?
                """,
                (json.dumps(combined, ensure_ascii=False), now, user_id, member_id),
            )
            conn.commit()
            return member_id

    def list_family_members(self, line_user_id: str) -> list[dict[str, Any]]:
        user_id = str(line_user_id or "").strip()
        if not user_id:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT canonical_name, aliases_json
                FROM family_registry_members
                WHERE line_user_id = ?
                ORDER BY created_at ASC, canonical_name ASC
                """,
                (user_id,),
            ).fetchall()

        members: list[dict[str, Any]] = []
        for row in rows:
            canonical_name = _normalize_family_name(row["canonical_name"])
            aliases_raw = _load_json(row["aliases_json"])
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

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT receipt_id, image_path
                FROM receipts
                WHERE line_user_id = ?
                """,
                (user_id,),
            ).fetchall()
            receipt_ids = [str(row["receipt_id"]) for row in rows]
            image_paths = [
                str(row["image_path"]).strip()
                for row in rows
                if str(row["image_path"]).strip()
            ]

            if receipt_ids:
                placeholders = ",".join("?" for _ in receipt_ids)
                conn.execute(
                    f"DELETE FROM receipt_fields WHERE receipt_id IN ({placeholders})",
                    tuple(receipt_ids),
                )
            conn.execute("DELETE FROM receipts WHERE line_user_id = ?", (user_id,))
            conn.execute("DELETE FROM conversation_sessions WHERE line_user_id = ?", (user_id,))
            conn.execute("DELETE FROM aggregate_entries WHERE line_user_id = ?", (user_id,))
            conn.execute("DELETE FROM family_registry_members WHERE line_user_id = ?", (user_id,))
            conn.execute("DELETE FROM family_registry_profiles WHERE line_user_id = ?", (user_id,))
            conn.execute("DELETE FROM correction_rules WHERE line_user_id = ?", (user_id,))
            conn.execute("DELETE FROM ocr_usage_guard WHERE scope_key = ?", (_ocr_guard_scope_user(user_id),))
            conn.commit()

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
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO correction_rules(
                    line_user_id, field_name, context_key, corrected_value, count, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(line_user_id, field_name, context_key, corrected_value)
                DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    normalized_field,
                    normalized_context,
                    normalized_value,
                    now,
                    now,
                ),
            )
            conn.commit()

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
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT corrected_value, count
                FROM correction_rules
                WHERE line_user_id = ?
                  AND field_name = ?
                  AND context_key = ?
                ORDER BY count DESC, updated_at DESC
                LIMIT 1
                """,
                (user_id, normalized_field, normalized_context),
            ).fetchone()
        if row is None:
            return None
        if int(row["count"] or 0) < threshold:
            return None
        value = str(row["corrected_value"] or "")
        if normalized_field == FieldName.PAYMENT_AMOUNT:
            return _to_int(value) if _to_int(value) is not None else value
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

        where_parts = ["line_user_id = ?"]
        params: list[Any] = [user_id]
        conditions: list[str] = []
        if image_hash:
            conditions.append("image_sha256 = ?")
            params.append(image_hash)
        if normalized_key:
            conditions.append("duplicate_key = ?")
            params.append(normalized_key)
        if not conditions:
            return []
        where_parts.append("(" + " OR ".join(conditions) + ")")
        sql = (
            "SELECT receipt_id, image_sha256, duplicate_key, created_at "
            "FROM receipts "
            f"WHERE {' AND '.join(where_parts)} "
            "ORDER BY created_at DESC "
            "LIMIT ?"
        )
        params.append(max_items * 2)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        duplicates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for row in rows:
            receipt_id = str(row["receipt_id"] or "").strip()
            if not receipt_id or receipt_id in seen_ids:
                continue
            seen_ids.add(receipt_id)
            reasons: list[str] = []
            if image_hash and str(row["image_sha256"] or "") == image_hash:
                reasons.append("image_sha256")
            if normalized_key and str(row["duplicate_key"] or "") == normalized_key:
                reasons.append("fields")
            duplicates.append(
                {
                    "receipt_id": receipt_id,
                    "created_at": str(row["created_at"] or ""),
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
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT receipt_id
                FROM receipts
                WHERE line_user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        receipt_id = str(row["receipt_id"] or "").strip()
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

        with self._connect() as conn:
            conn.execute("DELETE FROM ocr_usage_guard WHERE expires_at_epoch <= ?", (now_epoch,))

            for bucket in buckets:
                row = conn.execute(
                    """
                    SELECT count
                    FROM ocr_usage_guard
                    WHERE scope_key = ? AND window_key = ?
                    """,
                    (bucket["scope_key"], bucket["window_key"]),
                ).fetchone()
                current = int(row["count"] or 0) if row is not None else 0
                if current >= int(bucket["limit"]):
                    conn.commit()
                    return False, str(bucket["reason"])

            for bucket in buckets:
                row = conn.execute(
                    """
                    SELECT count
                    FROM ocr_usage_guard
                    WHERE scope_key = ? AND window_key = ?
                    """,
                    (bucket["scope_key"], bucket["window_key"]),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO ocr_usage_guard(scope_key, window_key, count, expires_at_epoch, updated_at)
                        VALUES(?, ?, ?, ?, ?)
                        """,
                        (
                            bucket["scope_key"],
                            bucket["window_key"],
                            1,
                            int(bucket["expires_at_epoch"]),
                            now_text,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE ocr_usage_guard
                        SET count = count + 1, updated_at = ?
                        WHERE scope_key = ? AND window_key = ?
                        """,
                        (now_text, bucket["scope_key"], bucket["window_key"]),
                    )
            conn.commit()
        return True, None

    def delete_receipt(self, line_user_id: str, receipt_id: str) -> str | None:
        user_id = str(line_user_id or "").strip()
        rid = str(receipt_id or "").strip()
        if not user_id or not rid:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT image_path
                FROM receipts
                WHERE receipt_id = ? AND line_user_id = ?
                """,
                (rid, user_id),
            ).fetchone()
            if row is None:
                return None
            image_path = str(row["image_path"] or "").strip() or None
            conn.execute("DELETE FROM receipt_fields WHERE receipt_id = ?", (rid,))
            conn.execute("DELETE FROM aggregate_entries WHERE receipt_id = ?", (rid,))
            conn.execute(
                "DELETE FROM conversation_sessions WHERE line_user_id = ? AND receipt_id = ?",
                (user_id, rid),
            )
            conn.execute(
                "DELETE FROM receipts WHERE receipt_id = ? AND line_user_id = ?",
                (rid, user_id),
            )
            conn.commit()
        return image_path


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
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
        value = json.loads(text)
    except Exception:
        return {}
    return value


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
    text = (str(value).strip() if value is not None else "")
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

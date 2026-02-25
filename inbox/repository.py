from __future__ import annotations

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
                """
            )
            conn.commit()

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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO receipts(
                    receipt_id, line_user_id, line_message_id, image_path, image_sha256,
                    document_id, decision_status, decision_confidence, processing_error,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    line_user_id,
                    line_message_id,
                    image_path,
                    image_sha256,
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

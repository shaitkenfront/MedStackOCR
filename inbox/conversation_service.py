from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

from core.enums import DecisionStatus, FieldName
from core.models import ExtractionResult
from inbox.models import ConversationSession
from inbox.repository_interface import InboxRepositoryProtocol
from inbox.state_machine import (
    STATE_AWAIT_CONFIRM,
    STATE_AWAIT_FIELD_CANDIDATE,
    STATE_AWAIT_FIELD_SELECTION,
    STATE_AWAIT_FREE_TEXT,
)
from linebot import message_templates

EDITABLE_FIELDS = {
    FieldName.PAYER_FACILITY_NAME,
    FieldName.PAYMENT_DATE,
    FieldName.PAYMENT_AMOUNT,
    FieldName.FAMILY_MEMBER_NAME,
}

FIELD_NAME_BY_LABEL = {
    "医療機関": FieldName.PAYER_FACILITY_NAME,
    "金額": FieldName.PAYMENT_AMOUNT,
    "日付": FieldName.PAYMENT_DATE,
    "対象者": FieldName.FAMILY_MEMBER_NAME,
    "名前": FieldName.FAMILY_MEMBER_NAME,
}


class ConversationService:
    def __init__(
        self,
        repository: InboxRepositoryProtocol,
        session_ttl_minutes: int = 60,
        max_candidate_options: int = 3,
    ) -> None:
        self.repository = repository
        self.session_ttl_minutes = max(5, int(session_ttl_minutes))
        self.max_candidate_options = max(1, int(max_candidate_options))

    def handle_new_result(
        self,
        line_user_id: str,
        receipt_id: str,
        result: ExtractionResult,
    ) -> list[dict[str, Any]]:
        fields = self._fields_from_result(result)
        candidates = self._candidates_from_result(result)
        learning_messages = self._apply_learning_hints(
            line_user_id=line_user_id,
            receipt_id=receipt_id,
            fields=fields,
        )
        decision = result.decision.status.value

        if decision == DecisionStatus.AUTO_ACCEPT.value:
            self.repository.upsert_aggregate_entry(
                receipt_id=receipt_id,
                line_user_id=line_user_id,
                fields=fields,
                status="tentative",
            )
            payload = {"fields": fields, "candidates": candidates, "decision": decision}
            self.repository.upsert_session(
                line_user_id=line_user_id,
                receipt_id=receipt_id,
                state=STATE_AWAIT_CONFIRM,
                payload=payload,
                expires_at=self._expires_at(),
            )
            messages = message_templates.build_auto_accept_message(receipt_id=receipt_id, fields=fields)
            messages.extend(learning_messages)
            return messages

        if decision == DecisionStatus.REVIEW_REQUIRED.value:
            self.repository.upsert_aggregate_entry(
                receipt_id=receipt_id,
                line_user_id=line_user_id,
                fields=fields,
                status="tentative",
            )
            payload = {"fields": fields, "candidates": candidates, "decision": decision}
            self.repository.upsert_session(
                line_user_id=line_user_id,
                receipt_id=receipt_id,
                state=STATE_AWAIT_CONFIRM,
                payload=payload,
                expires_at=self._expires_at(),
            )
            messages = message_templates.build_review_required_message(receipt_id=receipt_id, fields=fields)
            messages.extend(learning_messages)
            return messages

        self.repository.upsert_aggregate_entry(
            receipt_id=receipt_id,
            line_user_id=line_user_id,
            fields=fields,
            status="hold",
        )
        return message_templates.build_rejected_message()

    def handle_postback(self, line_user_id: str, data: str) -> list[dict[str, Any]]:
        params = self._parse_postback_data(data)
        action = params.get("a", "")
        receipt_id = params.get("r", "")
        field_name = params.get("f", "")
        index = self._safe_int(params.get("i"))

        session = self.repository.get_active_session(line_user_id)
        if action == "cancel":
            if session:
                self.repository.delete_session(session.session_id)
            return message_templates.build_cancelled_message()

        if action == "hold":
            if receipt_id:
                self.repository.set_aggregate_status(receipt_id, "hold")
            if session:
                self.repository.delete_session(session.session_id)
            return message_templates.build_hold_message()

        if action == "ok":
            if not receipt_id and session:
                receipt_id = session.receipt_id
            if not receipt_id:
                return message_templates.build_unknown_message()
            fields = self._session_fields(session) or self.repository.get_receipt_fields(receipt_id)
            self.repository.upsert_aggregate_entry(
                receipt_id=receipt_id,
                line_user_id=line_user_id,
                fields=fields,
                status="confirmed",
            )
            if session:
                self.repository.delete_session(session.session_id)
            messages = message_templates.build_confirmed_message(fields)
            messages.extend(self._build_cumulative_messages(line_user_id))
            return messages

        if action == "edit":
            session = self._ensure_session_for_edit(line_user_id=line_user_id, receipt_id=receipt_id, session=session)
            if session is None:
                return message_templates.build_unknown_message()
            self._save_session(
                session=session,
                state=STATE_AWAIT_FIELD_SELECTION,
                payload=session.payload,
                awaiting_field=None,
            )
            return message_templates.build_choose_field_message(session.receipt_id)

        if action == "field":
            if field_name not in EDITABLE_FIELDS:
                return message_templates.build_unknown_message()
            session = self._ensure_session_for_edit(line_user_id=line_user_id, receipt_id=receipt_id, session=session)
            if session is None:
                return message_templates.build_unknown_message()
            candidates = self._session_candidates(session).get(field_name, [])
            self._save_session(
                session=session,
                state=STATE_AWAIT_FIELD_CANDIDATE,
                payload=session.payload,
                awaiting_field=field_name,
            )
            return message_templates.build_choose_candidate_message(
                receipt_id=session.receipt_id,
                field_name=field_name,
                candidates=candidates,
            )

        if action == "pick":
            if session is None or session.state != STATE_AWAIT_FIELD_CANDIDATE:
                return message_templates.build_unknown_message()
            if field_name not in EDITABLE_FIELDS or index is None:
                return message_templates.build_unknown_message()
            fields = self._session_fields(session)
            candidates = self._session_candidates(session).get(field_name, [])
            if index < 0 or index >= len(candidates):
                return message_templates.build_unknown_message()
            fields[field_name] = candidates[index]
            session.payload["fields"] = fields
            self.repository.update_field_value(session.receipt_id, field_name, fields[field_name])
            self.repository.record_field_correction(
                line_user_id=line_user_id,
                field_name=field_name,
                context_key=self._learning_context_key(fields),
                corrected_value=fields[field_name],
            )
            self.repository.upsert_aggregate_entry(
                receipt_id=session.receipt_id,
                line_user_id=line_user_id,
                fields=fields,
                status="tentative",
            )
            self._save_session(
                session=session,
                state=STATE_AWAIT_CONFIRM,
                payload=session.payload,
                awaiting_field=None,
            )
            return message_templates.build_field_updated_message(
                receipt_id=session.receipt_id,
                fields=fields,
                field_name=field_name,
            )

        if action == "free_text":
            if session is None:
                return message_templates.build_unknown_message()
            if field_name not in EDITABLE_FIELDS:
                return message_templates.build_unknown_message()
            self._save_session(
                session=session,
                state=STATE_AWAIT_FREE_TEXT,
                payload=session.payload,
                awaiting_field=field_name,
            )
            label = message_templates.FIELD_LABELS.get(field_name, field_name)
            return [{"type": "text", "text": f"{label}の新しい値を入力してください。"}]

        if action == "back":
            if session is None:
                return message_templates.build_unknown_message()
            if session.state in {STATE_AWAIT_FIELD_CANDIDATE, STATE_AWAIT_FREE_TEXT}:
                self._save_session(
                    session=session,
                    state=STATE_AWAIT_FIELD_SELECTION,
                    payload=session.payload,
                    awaiting_field=None,
                )
                return message_templates.build_choose_field_message(session.receipt_id)
            self._save_session(
                session=session,
                state=STATE_AWAIT_CONFIRM,
                payload=session.payload,
                awaiting_field=None,
            )
            return self._build_confirm_prompt_message(session.receipt_id, self._session_fields(session), session.payload)

        return message_templates.build_unknown_message()

    def handle_text(self, line_user_id: str, text: str) -> list[dict[str, Any]]:
        session = self.repository.get_active_session(line_user_id)
        if session is None:
            return message_templates.build_unknown_message()

        normalized = (text or "").strip()
        if not normalized:
            return message_templates.build_unknown_message()

        if session.state == STATE_AWAIT_FREE_TEXT and session.awaiting_field in EDITABLE_FIELDS:
            field_name = str(session.awaiting_field)
            fields = self._session_fields(session)
            fields[field_name] = self._normalize_text_value(field_name, normalized)
            session.payload["fields"] = fields
            self.repository.update_field_value(session.receipt_id, field_name, fields[field_name])
            self.repository.record_field_correction(
                line_user_id=line_user_id,
                field_name=field_name,
                context_key=self._learning_context_key(fields),
                corrected_value=fields[field_name],
            )
            self.repository.upsert_aggregate_entry(
                receipt_id=session.receipt_id,
                line_user_id=line_user_id,
                fields=fields,
                status="tentative",
            )
            self._save_session(
                session=session,
                state=STATE_AWAIT_CONFIRM,
                payload=session.payload,
                awaiting_field=None,
            )
            return message_templates.build_field_updated_message(
                receipt_id=session.receipt_id,
                fields=fields,
                field_name=field_name,
            )

        action = self._action_from_text(normalized)
        if action is not None:
            return self.handle_postback(line_user_id, action)

        field_name = FIELD_NAME_BY_LABEL.get(normalized)
        if session.state == STATE_AWAIT_FIELD_SELECTION and field_name in EDITABLE_FIELDS:
            return self.handle_postback(line_user_id, f"a=field&r={session.receipt_id}&f={field_name}")
        return message_templates.build_unknown_message()

    @staticmethod
    def _fields_from_result(result: ExtractionResult) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key, candidate in result.fields.items():
            if candidate is None:
                continue
            value = candidate.value_normalized
            if value in (None, ""):
                value = candidate.value_raw
            fields[key] = value
        return fields

    def _candidates_from_result(self, result: ExtractionResult) -> dict[str, list[Any]]:
        output: dict[str, list[Any]] = {}
        for field_name, candidates in result.candidate_pool.items():
            seen: set[str] = set()
            options: list[Any] = []
            for candidate in candidates:
                value = candidate.value_normalized
                if value in (None, ""):
                    value = candidate.value_raw
                marker = str(value)
                if marker in seen:
                    continue
                seen.add(marker)
                options.append(value)
                if len(options) >= self.max_candidate_options:
                    break
            if options:
                output[field_name] = options
        return output

    @staticmethod
    def _parse_postback_data(data: str) -> dict[str, str]:
        parsed = parse_qs(str(data or ""), keep_blank_values=True)
        output: dict[str, str] = {}
        for key, values in parsed.items():
            if not values:
                continue
            output[key] = values[0]
        return output

    @staticmethod
    def _safe_int(text: str | None) -> int | None:
        if text is None:
            return None
        try:
            return int(text)
        except Exception:
            return None

    @staticmethod
    def _session_fields(session: ConversationSession | None) -> dict[str, Any]:
        if session is None:
            return {}
        payload = session.payload if isinstance(session.payload, dict) else {}
        fields = payload.get("fields", {})
        return dict(fields) if isinstance(fields, dict) else {}

    @staticmethod
    def _session_candidates(session: ConversationSession | None) -> dict[str, list[Any]]:
        if session is None:
            return {}
        payload = session.payload if isinstance(session.payload, dict) else {}
        raw = payload.get("candidates", {})
        if not isinstance(raw, dict):
            return {}
        output: dict[str, list[Any]] = {}
        for key, value in raw.items():
            if isinstance(value, list):
                output[key] = list(value)
        return output

    @staticmethod
    def _action_from_text(text: str) -> str | None:
        if text in {"ok", "OK", "はい", "登録", "確定"}:
            return "a=ok"
        if text in {"修正", "修正する"}:
            return "a=edit"
        if text in {"保留"}:
            return "a=hold"
        if text in {"キャンセル"}:
            return "a=cancel"
        if text in {"戻る"}:
            return "a=back"
        return None

    def _ensure_session_for_edit(
        self,
        line_user_id: str,
        receipt_id: str,
        session: ConversationSession | None,
    ) -> ConversationSession | None:
        rid = (receipt_id or "").strip()
        if session is not None:
            if not rid or session.receipt_id == rid:
                return session
            self.repository.delete_session(session.session_id)
            session = None
        if not rid:
            return None
        fields = self.repository.get_receipt_fields(rid)
        payload = {"fields": fields, "candidates": {}}
        session_id = self.repository.upsert_session(
            line_user_id=line_user_id,
            receipt_id=rid,
            state=STATE_AWAIT_FIELD_SELECTION,
            payload=payload,
            expires_at=self._expires_at(),
        )
        created = self.repository.get_active_session(line_user_id)
        if created is not None:
            return created
        if session_id:
            return ConversationSession(
                session_id=session_id,
                line_user_id=line_user_id,
                receipt_id=rid,
                state=STATE_AWAIT_FIELD_SELECTION,
                awaiting_field=None,
                payload=payload,
                expires_at=self._expires_at(),
                created_at="",
                updated_at="",
            )
        return None

    def _save_session(
        self,
        session: ConversationSession,
        state: str,
        payload: dict[str, Any],
        awaiting_field: str | None,
    ) -> None:
        self.repository.upsert_session(
            line_user_id=session.line_user_id,
            receipt_id=session.receipt_id,
            state=state,
            payload=payload,
            expires_at=self._expires_at(),
            awaiting_field=awaiting_field,
            session_id=session.session_id,
        )
        session.state = state
        session.payload = payload
        session.awaiting_field = awaiting_field

    @staticmethod
    def _decision_from_payload(payload: dict[str, Any]) -> str:
        value = payload.get("decision")
        return str(value) if value is not None else ""

    def _build_confirm_prompt_message(
        self,
        receipt_id: str,
        fields: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        data = payload if isinstance(payload, dict) else {}
        decision = self._decision_from_payload(data)
        if decision == DecisionStatus.AUTO_ACCEPT.value:
            return message_templates.build_auto_accept_message(receipt_id=receipt_id, fields=fields)
        return message_templates.build_review_required_message(receipt_id=receipt_id, fields=fields)

    def _expires_at(self) -> str:
        deadline = datetime.now(timezone.utc) + timedelta(minutes=self.session_ttl_minutes)
        return deadline.isoformat()

    def _build_cumulative_messages(self, line_user_id: str) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        current_year = now.year
        totals: list[tuple[int, int]] = []
        if now.month <= 3:
            prev_total, _ = self.repository.get_year_summary(line_user_id, current_year - 1)
            current_total, _ = self.repository.get_year_summary(line_user_id, current_year)
            totals.append((current_year - 1, prev_total))
            totals.append((current_year, current_total))
        else:
            current_total, _ = self.repository.get_year_summary(line_user_id, current_year)
            totals.append((current_year, current_total))
        return message_templates.build_yearly_cumulative_message(totals)

    def _apply_learning_hints(
        self,
        *,
        line_user_id: str,
        receipt_id: str,
        fields: dict[str, Any],
    ) -> list[dict[str, Any]]:
        context_key = self._learning_context_key(fields)
        if not context_key:
            return []

        field_name = FieldName.FAMILY_MEMBER_NAME
        hint = self.repository.get_field_correction_hint(
            line_user_id=line_user_id,
            field_name=field_name,
            context_key=context_key,
            min_count=2,
        )
        if hint in (None, ""):
            return []
        current = fields.get(field_name)
        if str(current or "") == str(hint):
            return []
        fields[field_name] = hint
        self.repository.update_field_value(receipt_id, field_name, hint, source="learning_hint")
        return [
            {
                "type": "text",
                "text": f"過去の訂正履歴を反映しました（対象者: {hint}）。必要なら修正してください。",
            }
        ]

    @staticmethod
    def _learning_context_key(fields: dict[str, Any]) -> str:
        facility = str(fields.get(FieldName.PAYER_FACILITY_NAME, "") or "").strip()
        if not facility:
            facility = str(fields.get(FieldName.PRESCRIBING_FACILITY_NAME, "") or "").strip()
        return facility

    @staticmethod
    def _normalize_text_value(field_name: str, value: str) -> Any:
        text = value.strip()
        if field_name == FieldName.PAYMENT_AMOUNT:
            digits = re.sub(r"[^\d\-]", "", text)
            if digits and digits not in {"-", "--"}:
                try:
                    return int(digits)
                except Exception:
                    return text
        return text

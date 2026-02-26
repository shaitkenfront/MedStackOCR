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

_FULL_DATE_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")
_MONTH_DAY_RE = re.compile(r"^(?P<month>\d{1,2})[\/\-](?P<day>\d{1,2})$")
_ENTRY_SPLIT_RE = re.compile(r"[\r\n]+")
_ALIAS_SPLIT_RE = re.compile(r"[,\u3001\uFF0C/\uFF0F|]+")
_WHITESPACE_RE = re.compile(r"\s+")
_FAMILY_REGISTRATION_AWAITING_FIELD = "__family_registration__"


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
        needs_date_confirmation, date_candidates = self._resolve_date_confirmation_candidates(
            fields=fields,
            candidates=candidates,
        )
        if needs_date_confirmation:
            candidates[FieldName.PAYMENT_DATE] = date_candidates
        needs_family_confirmation = self._needs_family_member_confirmation(result)
        family_candidates: list[Any] = []
        if needs_family_confirmation:
            family_candidates = self._resolve_family_member_candidates(
                line_user_id=line_user_id,
                fields=fields,
                candidates=candidates,
            )
            candidates[FieldName.FAMILY_MEMBER_NAME] = family_candidates
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
                state=STATE_AWAIT_FIELD_CANDIDATE if needs_date_confirmation else STATE_AWAIT_CONFIRM,
                payload=payload,
                expires_at=self._expires_at(),
                awaiting_field=FieldName.PAYMENT_DATE if needs_date_confirmation else None,
            )
            messages = message_templates.build_auto_accept_message(receipt_id=receipt_id, fields=fields)
            if needs_date_confirmation:
                messages.append(
                    {
                        "type": "text",
                        "text": "日付の年が特定できないため、候補から選択してください。",
                    }
                )
                messages.extend(
                    message_templates.build_choose_candidate_message(
                        receipt_id=receipt_id,
                        field_name=FieldName.PAYMENT_DATE,
                        candidates=date_candidates,
                    )
                )
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
            review_state = STATE_AWAIT_CONFIRM
            review_awaiting_field: str | None = None
            if needs_family_confirmation:
                review_state = STATE_AWAIT_FIELD_CANDIDATE
                review_awaiting_field = FieldName.FAMILY_MEMBER_NAME
            elif needs_date_confirmation:
                review_state = STATE_AWAIT_FIELD_CANDIDATE
                review_awaiting_field = FieldName.PAYMENT_DATE
            self.repository.upsert_session(
                line_user_id=line_user_id,
                receipt_id=receipt_id,
                state=review_state,
                payload=payload,
                expires_at=self._expires_at(),
                awaiting_field=review_awaiting_field,
            )
            messages = message_templates.build_review_required_message(receipt_id=receipt_id, fields=fields)
            if needs_family_confirmation:
                messages.append(
                    {
                        "type": "text",
                        "text": "対象者が未登録のため、登録済みの家族から選択してください。",
                    }
                )
                messages.extend(
                    message_templates.build_choose_candidate_message(
                        receipt_id=receipt_id,
                        field_name=FieldName.FAMILY_MEMBER_NAME,
                        candidates=family_candidates,
                        include_add_family_action=True,
                    )
                )
            elif needs_date_confirmation:
                messages.append(
                    {
                        "type": "text",
                        "text": "日付の年が特定できないため、候補から選択してください。",
                    }
                )
                messages.extend(
                    message_templates.build_choose_candidate_message(
                        receipt_id=receipt_id,
                        field_name=FieldName.PAYMENT_DATE,
                        candidates=date_candidates,
                    )
                )
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

        if action == "add_family":
            if session is None:
                return message_templates.build_unknown_message()
            if session.awaiting_field != FieldName.FAMILY_MEMBER_NAME:
                return message_templates.build_unknown_message()
            payload = dict(session.payload) if isinstance(session.payload, dict) else {}
            payload["family_registration_resume"] = {
                "state": session.state,
                "awaiting_field": session.awaiting_field,
            }
            self._save_session(
                session=session,
                state=STATE_AWAIT_FREE_TEXT,
                payload=payload,
                awaiting_field=_FAMILY_REGISTRATION_AWAITING_FIELD,
            )
            return message_templates.build_family_registration_prompt_message()

        if action == "pick":
            if session is None or session.state != STATE_AWAIT_FIELD_CANDIDATE:
                return message_templates.build_unknown_message()
            if field_name not in EDITABLE_FIELDS or index is None:
                return message_templates.build_unknown_message()
            fields = self._session_fields(session)
            candidates = self._session_candidates(session).get(field_name, [])
            if index < 0 or index >= len(candidates):
                return message_templates.build_unknown_message()
            previous_value = fields.get(field_name)
            fields[field_name] = candidates[index]
            session.payload["fields"] = fields
            self.repository.update_field_value(session.receipt_id, field_name, fields[field_name])
            if field_name == FieldName.FAMILY_MEMBER_NAME:
                self._add_family_alias_if_needed(
                    line_user_id=line_user_id,
                    raw_value=previous_value,
                    corrected_value=fields[field_name],
                )
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
            previous_value = fields.get(field_name)
            fields[field_name] = self._normalize_text_value(field_name, normalized)
            session.payload["fields"] = fields
            self.repository.update_field_value(session.receipt_id, field_name, fields[field_name])
            if field_name == FieldName.FAMILY_MEMBER_NAME:
                self._add_family_alias_if_needed(
                    line_user_id=line_user_id,
                    raw_value=previous_value,
                    corrected_value=fields[field_name],
                )
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

        if (
            session.state == STATE_AWAIT_FREE_TEXT
            and str(session.awaiting_field or "") == _FAMILY_REGISTRATION_AWAITING_FIELD
        ):
            return self._handle_family_registration_text(
                line_user_id=line_user_id,
                session=session,
                text=normalized,
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

    @staticmethod
    def _needs_family_member_confirmation(result: ExtractionResult) -> bool:
        if result.decision.status.value != DecisionStatus.REVIEW_REQUIRED.value:
            return False
        candidate = result.fields.get(FieldName.FAMILY_MEMBER_NAME)
        if candidate is None:
            return False
        source = str(getattr(candidate, "source", "") or "").strip()
        return source in {"family_registry_same_surname", "family_registry_unknown_surname"}

    def _resolve_family_member_candidates(
        self,
        *,
        line_user_id: str,
        fields: dict[str, Any],
        candidates: dict[str, list[Any]],
    ) -> list[Any]:
        options: list[str] = []
        seen: set[str] = set()
        max_options = max(1, self.max_candidate_options)

        def add_option(value: Any) -> None:
            normalized = self._normalize_family_name(value)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            options.append(normalized)

        for member in self.repository.list_family_members(line_user_id):
            add_option(member.get("canonical_name"))
            if len(options) >= max_options:
                return options
        for value in [fields.get(FieldName.FAMILY_MEMBER_NAME), *candidates.get(FieldName.FAMILY_MEMBER_NAME, [])]:
            add_option(value)
            if len(options) >= max_options:
                return options
        return options

    def _add_family_alias_if_needed(
        self,
        *,
        line_user_id: str,
        raw_value: Any,
        corrected_value: Any,
    ) -> None:
        alias = self._normalize_family_name(raw_value)
        canonical = self._normalize_family_name(corrected_value)
        if not alias or not canonical or alias == canonical:
            return
        self.repository.upsert_family_member(
            line_user_id=line_user_id,
            canonical_name=canonical,
            aliases=[alias],
        )

    def _handle_family_registration_text(
        self,
        *,
        line_user_id: str,
        session: ConversationSession,
        text: str,
    ) -> list[dict[str, Any]]:
        normalized = str(text or "").strip()
        if not normalized:
            return message_templates.build_family_registration_prompt_message()

        finish_keyword = message_templates.FAMILY_REGISTRATION_FINISH_TEXT
        if normalized == finish_keyword:
            members = self.repository.list_family_members(line_user_id)
            if not members:
                return message_templates.build_family_registration_need_member_message()
            self.repository.complete_family_registration(line_user_id)
            return self._resume_after_family_registration(session=session, members=members)

        entries = self._parse_family_registration_entries(normalized)
        if not entries:
            return message_templates.build_family_registration_prompt_message()
        invalid_names = self._collect_missing_name_separator_canonicals(entries)
        if invalid_names:
            return message_templates.build_family_registration_need_space_message(invalid_names)

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
        return message_templates.build_family_registration_saved_message(len(members), latest_names)

    def _resume_after_family_registration(
        self,
        *,
        session: ConversationSession,
        members: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        payload = dict(session.payload) if isinstance(session.payload, dict) else {}
        raw_resume = payload.get("family_registration_resume")
        payload.pop("family_registration_resume", None)
        resume_state = STATE_AWAIT_FIELD_CANDIDATE
        resume_awaiting = FieldName.FAMILY_MEMBER_NAME
        if isinstance(raw_resume, dict):
            state = str(raw_resume.get("state", "") or "")
            awaiting = str(raw_resume.get("awaiting_field", "") or "")
            if state == STATE_AWAIT_FIELD_CANDIDATE and awaiting == FieldName.FAMILY_MEMBER_NAME:
                resume_state = state
                resume_awaiting = awaiting

        fields = self._session_fields(session)
        candidates = self._session_candidates(session)
        members_candidates = self._resolve_family_member_candidates(
            line_user_id=session.line_user_id,
            fields=fields,
            candidates=candidates,
        )
        candidates[FieldName.FAMILY_MEMBER_NAME] = members_candidates
        payload["fields"] = fields
        payload["candidates"] = candidates
        self._save_session(
            session=session,
            state=resume_state,
            payload=payload,
            awaiting_field=resume_awaiting,
        )
        messages = message_templates.build_family_registration_completed_message(len(members))
        messages.extend(
            message_templates.build_choose_candidate_message(
                receipt_id=session.receipt_id,
                field_name=FieldName.FAMILY_MEMBER_NAME,
                candidates=members_candidates,
                include_add_family_action=True,
            )
        )
        return messages

    @staticmethod
    def _normalize_family_name(value: Any) -> str:
        text = str(value or "").replace("\u3000", " ").strip()
        if not text:
            return ""
        return _WHITESPACE_RE.sub(" ", text)

    @staticmethod
    def _parse_family_registration_entries(text: str) -> list[tuple[str, list[str]]]:
        rows = [row.strip() for row in _ENTRY_SPLIT_RE.split(str(text or "")) if row.strip()]
        entries: list[tuple[str, list[str]]] = []
        for row in rows:
            parts = [ConversationService._normalize_family_name(part) for part in _ALIAS_SPLIT_RE.split(row)]
            names = [name for name in parts if name]
            if not names:
                continue
            canonical_name = names[0]
            aliases = ConversationService._dedupe_family_names(names[1:])
            entries.append((canonical_name, aliases))
        return entries

    @staticmethod
    def _dedupe_family_names(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = ConversationService._normalize_family_name(value)
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(normalized)
        return result

    @staticmethod
    def _collect_missing_name_separator_canonicals(entries: list[tuple[str, list[str]]]) -> list[str]:
        invalid: list[str] = []
        for canonical_name, _ in entries:
            if ConversationService._has_family_given_space(canonical_name):
                continue
            if canonical_name not in invalid:
                invalid.append(canonical_name)
        return invalid

    @staticmethod
    def _has_family_given_space(name: str) -> bool:
        normalized = ConversationService._normalize_family_name(name)
        if not normalized or " " not in normalized:
            return False
        parts = normalized.split(" ")
        return len(parts) >= 2 and bool(parts[0]) and bool(parts[1])

    def _resolve_date_confirmation_candidates(
        self,
        *,
        fields: dict[str, Any],
        candidates: dict[str, list[Any]],
    ) -> tuple[bool, list[Any]]:
        raw_date = fields.get(FieldName.PAYMENT_DATE)
        normalized = self._normalize_date_candidate(raw_date)
        if normalized is None:
            return False, []
        if _FULL_DATE_RE.match(normalized):
            return False, []

        month_day = self._extract_month_day(normalized)
        if month_day is None:
            return False, []

        year = datetime.now(timezone.utc).year
        option_years = [year, year - 1]
        options: list[str] = []
        seen: set[str] = set()

        for value in [raw_date, *candidates.get(FieldName.PAYMENT_DATE, [])]:
            normalized_value = self._normalize_date_candidate(value)
            if normalized_value is None:
                continue
            full_match = _FULL_DATE_RE.match(normalized_value)
            if full_match is not None:
                if normalized_value in seen:
                    continue
                seen.add(normalized_value)
                options.append(normalized_value)
                continue
            md = self._extract_month_day(normalized_value)
            if md is None:
                continue
            month, day = md
            for candidate_year in option_years:
                candidate = f"{candidate_year:04d}-{month:02d}-{day:02d}"
                if candidate in seen:
                    continue
                seen.add(candidate)
                options.append(candidate)
                if len(options) >= self.max_candidate_options:
                    return True, options
        return True, options

    @staticmethod
    def _normalize_date_candidate(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        full = _FULL_DATE_RE.match(text)
        if full is not None:
            return text
        month_day = _MONTH_DAY_RE.match(text)
        if month_day is not None:
            month = int(month_day.group("month"))
            day = int(month_day.group("day"))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{month:02d}-{day:02d}"
        return None

    @staticmethod
    def _extract_month_day(value: str) -> tuple[int, int] | None:
        month_day = _MONTH_DAY_RE.match(value)
        if month_day is None:
            return None
        month = int(month_day.group("month"))
        day = int(month_day.group("day"))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return month, day

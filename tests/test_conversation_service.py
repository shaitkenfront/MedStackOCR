from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.enums import DecisionStatus, DocumentType, FieldName
from core.models import AuditInfo, Candidate, Decision, ExtractionResult, TemplateMatch
from inbox.conversation_service import ConversationService
from inbox.repository import InboxRepository
from linebot import message_templates


def _candidate(field: str, value: object, score: float = 5.0, source: str = "generic") -> Candidate:
    return Candidate(
        field=field,
        value_raw=value,
        value_normalized=value,
        source_line_indices=[0],
        bbox=None,
        score=score,
        ocr_confidence=0.9,
        reasons=["test"],
        source=source,
    )


class ConversationServiceTest(unittest.TestCase):
    def test_review_flow_pick_and_confirm(self) -> None:
        year = datetime.now(timezone.utc).year
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            service = ConversationService(repo, session_ttl_minutes=60, max_candidate_options=3)
            result = ExtractionResult(
                document_id="doc1",
                household_id=None,
                document_type=DocumentType.PHARMACY,
                template_match=TemplateMatch(matched=False, template_family_id=None, score=0.0),
                fields={
                    FieldName.PAYER_FACILITY_NAME: _candidate(FieldName.PAYER_FACILITY_NAME, "テスト薬局"),
                    FieldName.PAYMENT_DATE: _candidate(FieldName.PAYMENT_DATE, f"{year}-02-20"),
                    FieldName.PAYMENT_AMOUNT: _candidate(FieldName.PAYMENT_AMOUNT, 1000),
                    FieldName.FAMILY_MEMBER_NAME: _candidate(FieldName.FAMILY_MEMBER_NAME, "山田 太郎"),
                },
                decision=Decision(
                    status=DecisionStatus.REVIEW_REQUIRED,
                    confidence=0.7,
                    reasons=["test"],
                ),
                audit=AuditInfo(engine="mock", engine_version="1.0", pipeline_version="0.1.0"),
                candidate_pool={
                    FieldName.PAYMENT_AMOUNT: [
                        _candidate(FieldName.PAYMENT_AMOUNT, 1000),
                        _candidate(FieldName.PAYMENT_AMOUNT, 1200),
                    ]
                },
                ocr_lines=[],
            )

            service.handle_new_result("U1", "R1", result)
            session = repo.get_active_session("U1")
            self.assertIsNotNone(session)

            candidate_messages = service.handle_postback("U1", f"a=field&r=R1&f={FieldName.PAYMENT_AMOUNT}")
            candidate_labels = _extract_quick_reply_labels(candidate_messages)
            self.assertIn("自分で入力する", candidate_labels)
            service.handle_postback("U1", f"a=pick&r=R1&f={FieldName.PAYMENT_AMOUNT}&i=1")
            fields = repo.get_receipt_fields("R1")
            self.assertEqual(fields.get(FieldName.PAYMENT_AMOUNT), 1200)

            messages = service.handle_postback("U1", "a=ok&r=R1")
            self.assertIsNone(repo.get_active_session("U1"))
            total, count = repo.get_year_summary("U1", year)
            self.assertEqual((total, count), (1200, 1))
            _assert_cumulative_message(messages, expected_current_year_total=1200)

    def test_auto_accept_waits_user_confirmation(self) -> None:
        year = datetime.now(timezone.utc).year
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            service = ConversationService(repo)
            result = ExtractionResult(
                document_id="doc2",
                household_id=None,
                document_type=DocumentType.CLINIC_OR_HOSPITAL,
                template_match=TemplateMatch(matched=False, template_family_id=None, score=0.0),
                fields={
                    FieldName.PAYER_FACILITY_NAME: _candidate(FieldName.PAYER_FACILITY_NAME, "テスト医院"),
                    FieldName.PAYMENT_DATE: _candidate(FieldName.PAYMENT_DATE, f"{year}-01-01"),
                    FieldName.PAYMENT_AMOUNT: _candidate(FieldName.PAYMENT_AMOUNT, 3500),
                    FieldName.FAMILY_MEMBER_NAME: _candidate(FieldName.FAMILY_MEMBER_NAME, "山田 花子"),
                },
                decision=Decision(status=DecisionStatus.AUTO_ACCEPT, confidence=0.95, reasons=["test"]),
                audit=AuditInfo(engine="mock", engine_version="1.0", pipeline_version="0.1.0"),
                candidate_pool={},
                ocr_lines=[],
            )
            messages = service.handle_new_result("U2", "R2", result)
            session = repo.get_active_session("U2")
            self.assertIsNotNone(session)
            self.assertEqual(session.state, "AWAIT_CONFIRM")
            total, count = repo.get_year_summary("U2", year)
            self.assertEqual((total, count), (3500, 1))
            self.assertEqual(repo.get_pending_count("U2"), 1)
            joined = "\n".join(str(m.get("text", "")) for m in messages if isinstance(m, dict))
            self.assertIn("内容を確認してください。", joined)
            self.assertIn(f"日付: {year}/01/01", joined)
            quick_reply_labels = _extract_quick_reply_labels(messages)
            self.assertIn("取り消し", quick_reply_labels)

            confirmed_messages = service.handle_postback("U2", "a=ok&r=R2")
            self.assertIsNone(repo.get_active_session("U2"))
            self.assertEqual(repo.get_pending_count("U2"), 0)
            _assert_cumulative_message(confirmed_messages, expected_current_year_total=3500)

    def test_learning_hint_updates_family_member_name(self) -> None:
        year = datetime.now(timezone.utc).year
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            service = ConversationService(repo)
            repo.record_field_correction("U3", FieldName.FAMILY_MEMBER_NAME, "テスト医院", "山田 花子")
            repo.record_field_correction("U3", FieldName.FAMILY_MEMBER_NAME, "テスト医院", "山田 花子")

            result = ExtractionResult(
                document_id="doc3",
                household_id=None,
                document_type=DocumentType.CLINIC_OR_HOSPITAL,
                template_match=TemplateMatch(matched=False, template_family_id=None, score=0.0),
                fields={
                    FieldName.PAYER_FACILITY_NAME: _candidate(FieldName.PAYER_FACILITY_NAME, "テスト医院"),
                    FieldName.PAYMENT_DATE: _candidate(FieldName.PAYMENT_DATE, f"{year}-03-03"),
                    FieldName.PAYMENT_AMOUNT: _candidate(FieldName.PAYMENT_AMOUNT, 2000),
                    FieldName.FAMILY_MEMBER_NAME: _candidate(FieldName.FAMILY_MEMBER_NAME, "山田 太郎"),
                },
                decision=Decision(status=DecisionStatus.AUTO_ACCEPT, confidence=0.95, reasons=["test"]),
                audit=AuditInfo(engine="mock", engine_version="1.0", pipeline_version="0.1.0"),
                candidate_pool={},
                ocr_lines=[],
            )

            messages = service.handle_new_result("U3", "R3", result)
            fields = repo.get_receipt_fields("R3")
            self.assertEqual(fields.get(FieldName.FAMILY_MEMBER_NAME), "山田 花子")
            joined = "\n".join(str(m.get("text", "")) for m in messages if isinstance(m, dict))
            self.assertIn("過去の訂正履歴を反映", joined)

    def test_incomplete_payment_date_prompts_candidate_confirmation(self) -> None:
        year = datetime.now(timezone.utc).year
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            service = ConversationService(repo, session_ttl_minutes=60, max_candidate_options=3)
            result = ExtractionResult(
                document_id="doc4",
                household_id=None,
                document_type=DocumentType.CLINIC_OR_HOSPITAL,
                template_match=TemplateMatch(matched=False, template_family_id=None, score=0.0),
                fields={
                    FieldName.PAYER_FACILITY_NAME: _candidate(FieldName.PAYER_FACILITY_NAME, "外来センター病院"),
                    FieldName.PAYMENT_DATE: _candidate(FieldName.PAYMENT_DATE, "02-17"),
                    FieldName.PAYMENT_AMOUNT: _candidate(FieldName.PAYMENT_AMOUNT, 11860),
                    FieldName.FAMILY_MEMBER_NAME: _candidate(FieldName.FAMILY_MEMBER_NAME, "山田 太郎"),
                },
                decision=Decision(status=DecisionStatus.AUTO_ACCEPT, confidence=0.95, reasons=["test"]),
                audit=AuditInfo(engine="mock", engine_version="1.0", pipeline_version="0.1.0"),
                candidate_pool={
                    FieldName.PAYMENT_DATE: [
                        _candidate(FieldName.PAYMENT_DATE, "02-17"),
                    ]
                },
                ocr_lines=[],
            )
            messages = service.handle_new_result("U4", "R4", result)
            session = repo.get_active_session("U4")
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.state, "AWAIT_FIELD_CANDIDATE")
            self.assertEqual(session.awaiting_field, FieldName.PAYMENT_DATE)
            joined = "\n".join(str(m.get("text", "")) for m in messages if isinstance(m, dict))
            self.assertIn("日付の年が特定できないため", joined)
            labels = _extract_quick_reply_labels(messages)
            self.assertIn(f"{year}/02/17", labels)

            service.handle_postback("U4", f"a=pick&r=R4&f={FieldName.PAYMENT_DATE}&i=0")
            fields = repo.get_receipt_fields("R4")
            self.assertEqual(fields.get(FieldName.PAYMENT_DATE), f"{year}-02-17")

    def test_review_required_family_name_shows_registered_candidates(self) -> None:
        year = datetime.now(timezone.utc).year
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            service = ConversationService(repo, session_ttl_minutes=60, max_candidate_options=3)
            repo.ensure_family_registration_started("U5")
            repo.upsert_family_member("U5", "山田 太郎", ["ヤマダ タロウ"])
            repo.complete_family_registration("U5")

            result = ExtractionResult(
                document_id="doc5",
                household_id=None,
                document_type=DocumentType.CLINIC_OR_HOSPITAL,
                template_match=TemplateMatch(matched=False, template_family_id=None, score=0.0),
                fields={
                    FieldName.PAYER_FACILITY_NAME: _candidate(FieldName.PAYER_FACILITY_NAME, "テスト医院"),
                    FieldName.PAYMENT_DATE: _candidate(FieldName.PAYMENT_DATE, f"{year}-02-20"),
                    FieldName.PAYMENT_AMOUNT: _candidate(FieldName.PAYMENT_AMOUNT, 4200),
                    FieldName.FAMILY_MEMBER_NAME: _candidate(
                        FieldName.FAMILY_MEMBER_NAME,
                        "山田 太朗",
                        source="family_registry_same_surname",
                    ),
                },
                decision=Decision(status=DecisionStatus.REVIEW_REQUIRED, confidence=0.7, reasons=["test"]),
                audit=AuditInfo(engine="mock", engine_version="1.0", pipeline_version="0.1.0"),
                candidate_pool={},
                ocr_lines=[],
            )
            messages = service.handle_new_result("U5", "R5", result)
            session = repo.get_active_session("U5")
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.state, "AWAIT_FIELD_CANDIDATE")
            self.assertEqual(session.awaiting_field, FieldName.FAMILY_MEMBER_NAME)
            labels = _extract_quick_reply_labels(messages)
            self.assertIn("山田 太郎", labels)
            self.assertIn("新しい家族を追加", labels)

            service.handle_postback("U5", f"a=pick&r=R5&f={FieldName.FAMILY_MEMBER_NAME}&i=0")
            fields = repo.get_receipt_fields("R5")
            self.assertEqual(fields.get(FieldName.FAMILY_MEMBER_NAME), "山田 太郎")
            members = repo.list_family_members("U5")
            self.assertIn("山田 太朗", members[0]["aliases"])

    def test_add_family_flow_returns_to_original_review(self) -> None:
        year = datetime.now(timezone.utc).year
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            service = ConversationService(repo, session_ttl_minutes=60, max_candidate_options=3)
            repo.ensure_family_registration_started("U6")
            repo.upsert_family_member("U6", "山田 太郎", ["ヤマダ タロウ"])
            repo.complete_family_registration("U6")

            result = ExtractionResult(
                document_id="doc6",
                household_id=None,
                document_type=DocumentType.CLINIC_OR_HOSPITAL,
                template_match=TemplateMatch(matched=False, template_family_id=None, score=0.0),
                fields={
                    FieldName.PAYER_FACILITY_NAME: _candidate(FieldName.PAYER_FACILITY_NAME, "テスト医院"),
                    FieldName.PAYMENT_DATE: _candidate(FieldName.PAYMENT_DATE, f"{year}-02-21"),
                    FieldName.PAYMENT_AMOUNT: _candidate(FieldName.PAYMENT_AMOUNT, 5200),
                    FieldName.FAMILY_MEMBER_NAME: _candidate(
                        FieldName.FAMILY_MEMBER_NAME,
                        "佐藤 花了",
                        source="family_registry_unknown_surname",
                    ),
                },
                decision=Decision(status=DecisionStatus.REVIEW_REQUIRED, confidence=0.7, reasons=["test"]),
                audit=AuditInfo(engine="mock", engine_version="1.0", pipeline_version="0.1.0"),
                candidate_pool={},
                ocr_lines=[],
            )
            messages = service.handle_new_result("U6", "R6", result)
            labels = _extract_quick_reply_labels(messages)
            self.assertIn("新しい家族を追加", labels)

            start_messages = service.handle_postback("U6", "a=add_family&r=R6")
            self.assertIn("ご家族の名前を教えてください", str(start_messages[0].get("text", "")))
            session = repo.get_active_session("U6")
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.state, "AWAIT_FREE_TEXT")
            self.assertEqual(session.awaiting_field, "__family_registration__")

            saved_messages = service.handle_text("U6", "佐藤 花子, サトウ ハナコ")
            saved_joined = "\n".join(str(m.get("text", "")) for m in saved_messages if isinstance(m, dict))
            self.assertIn("登録済み", saved_joined)

            finish_messages = service.handle_text("U6", message_templates.FAMILY_REGISTRATION_FINISH_TEXT)
            finish_labels = _extract_quick_reply_labels(finish_messages)
            self.assertIn("佐藤 花子", finish_labels)
            self.assertIn("新しい家族を追加", finish_labels)

            resumed_session = repo.get_active_session("U6")
            self.assertIsNotNone(resumed_session)
            assert resumed_session is not None
            self.assertEqual(resumed_session.state, "AWAIT_FIELD_CANDIDATE")
            self.assertEqual(resumed_session.awaiting_field, FieldName.FAMILY_MEMBER_NAME)
            options = resumed_session.payload.get("candidates", {}).get(FieldName.FAMILY_MEMBER_NAME, [])
            index = options.index("佐藤 花子")
            service.handle_postback("U6", f"a=pick&r=R6&f={FieldName.FAMILY_MEMBER_NAME}&i={index}")
            fields = repo.get_receipt_fields("R6")
            self.assertEqual(fields.get(FieldName.FAMILY_MEMBER_NAME), "佐藤 花子")

            members = repo.list_family_members("U6")
            sato = [item for item in members if item.get("canonical_name") == "佐藤 花子"]
            self.assertTrue(sato)
            self.assertIn("佐藤 花了", sato[0]["aliases"])


def _extract_quick_reply_labels(messages: list[dict[str, object]]) -> list[str]:
    labels: list[str] = []
    for message in messages:
        quick_reply = message.get("quickReply") if isinstance(message, dict) else None
        if not isinstance(quick_reply, dict):
            continue
        items = quick_reply.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            action = item.get("action")
            if not isinstance(action, dict):
                continue
            label = str(action.get("label", "") or "").strip()
            if label:
                labels.append(label)
    return labels


def _assert_cumulative_message(messages: list[dict[str, object]], expected_current_year_total: int) -> None:
    now = datetime.now(timezone.utc)
    year = now.year
    text_messages = [str(message.get("text", "")) for message in messages if isinstance(message, dict)]
    joined = "\n".join(text_messages)
    if now.month <= 3:
        assert f"{year - 1}年の累計医療費: 0円" in joined
        assert f"{year}年の累計医療費: {expected_current_year_total:,}円" in joined
    else:
        assert f"{year}年の累計医療費: {expected_current_year_total:,}円" in joined


if __name__ == "__main__":
    unittest.main()

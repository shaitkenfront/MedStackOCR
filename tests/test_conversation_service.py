from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.enums import DecisionStatus, DocumentType, FieldName
from core.models import AuditInfo, Candidate, Decision, ExtractionResult, TemplateMatch
from inbox.conversation_service import ConversationService
from inbox.repository import InboxRepository


def _candidate(field: str, value: object, score: float = 5.0) -> Candidate:
    return Candidate(
        field=field,
        value_raw=value,
        value_normalized=value,
        source_line_indices=[0],
        bbox=None,
        score=score,
        ocr_confidence=0.9,
        reasons=["test"],
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

            service.handle_postback("U1", f"a=field&r=R1&f={FieldName.PAYMENT_AMOUNT}")
            service.handle_postback("U1", f"a=pick&r=R1&f={FieldName.PAYMENT_AMOUNT}&i=1")
            fields = repo.get_receipt_fields("R1")
            self.assertEqual(fields.get(FieldName.PAYMENT_AMOUNT), 1200)

            messages = service.handle_postback("U1", "a=ok&r=R1")
            self.assertIsNone(repo.get_active_session("U1"))
            total, count = repo.get_year_summary("U1", year)
            self.assertEqual((total, count), (1200, 1))
            _assert_cumulative_message(messages, expected_current_year_total=1200)

    def test_auto_accept_sets_confirmed(self) -> None:
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
            self.assertIsNone(repo.get_active_session("U2"))
            total, count = repo.get_year_summary("U2", year)
            self.assertEqual((total, count), (3500, 1))
            self.assertEqual(repo.get_pending_count("U2"), 0)
            _assert_cumulative_message(messages, expected_current_year_total=3500)


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

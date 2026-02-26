from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.enums import DecisionStatus, DocumentType, FieldName
from core.models import AuditInfo, Candidate, Decision, ExtractionResult, TemplateMatch
from inbox.repository import InboxRepository


class InboxRepositoryTest(unittest.TestCase):
    def test_mark_event_processed_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            self.assertTrue(repo.mark_event_processed("evt-1"))
            self.assertFalse(repo.mark_event_processed("evt-1"))

    def test_session_upsert_and_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            sid = repo.upsert_session(
                line_user_id="U1",
                receipt_id="R1",
                state="AWAIT_CONFIRM",
                payload={"fields": {"payment_amount": 1200}},
                expires_at="2999-01-01T00:00:00+00:00",
            )
            session = repo.get_active_session("U1")
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.session_id, sid)
            self.assertEqual(session.receipt_id, "R1")
            self.assertEqual(session.payload["fields"]["payment_amount"], 1200)

    def test_aggregate_summary(self) -> None:
        year = datetime.now(timezone.utc).year
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            fields = {
                FieldName.PAYMENT_DATE: f"{year}-02-25",
                FieldName.PAYMENT_AMOUNT: 1500,
                FieldName.PAYER_FACILITY_NAME: "テスト医院",
                FieldName.FAMILY_MEMBER_NAME: "山田 太郎",
            }
            repo.upsert_aggregate_entry(
                receipt_id="R1",
                line_user_id="U1",
                fields=fields,
                status="tentative",
            )
            total_y, count_y = repo.get_year_summary("U1", year)
            total_m, count_m = repo.get_month_summary("U1", year, 2)
            self.assertEqual((total_y, count_y), (1500, 1))
            self.assertEqual((total_m, count_m), (1500, 1))
            self.assertEqual(repo.get_pending_count("U1"), 1)
            repo.set_aggregate_status("R1", "confirmed")
            self.assertEqual(repo.get_pending_count("U1"), 0)

    def test_update_field_value_and_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            repo.update_field_value("R1", FieldName.PAYMENT_AMOUNT, "2,480円")
            fields = repo.get_receipt_fields("R1")
            self.assertEqual(fields.get(FieldName.PAYMENT_AMOUNT), 2480)

    def test_family_registration_state_and_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            self.assertFalse(repo.is_family_registration_completed("U1"))
            self.assertTrue(repo.ensure_family_registration_started("U1"))
            self.assertFalse(repo.ensure_family_registration_started("U1"))

            member_id = repo.upsert_family_member(
                line_user_id="U1",
                canonical_name="山田 太郎",
                aliases=["ヤマダ タロウ"],
            )
            self.assertTrue(member_id)
            repo.upsert_family_member(
                line_user_id="U1",
                canonical_name="山田 太郎",
                aliases=["山田太郎", "ヤマダ タロウ"],
            )
            members = repo.list_family_members("U1")
            self.assertEqual(len(members), 1)
            self.assertEqual(members[0]["canonical_name"], "山田 太郎")
            self.assertIn("ヤマダ タロウ", members[0]["aliases"])
            self.assertIn("山田太郎", members[0]["aliases"])

            repo.complete_family_registration("U1")
            self.assertTrue(repo.is_family_registration_completed("U1"))

    def test_purge_user_data_removes_related_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            repo.ensure_family_registration_started("U1")
            repo.upsert_family_member("U1", "山田 太郎", ["ヤマダ タロウ"])
            repo.upsert_session(
                line_user_id="U1",
                receipt_id="R1",
                state="AWAIT_CONFIRM",
                payload={},
                expires_at="2999-01-01T00:00:00+00:00",
            )
            repo.upsert_aggregate_entry(
                receipt_id="R1",
                line_user_id="U1",
                fields={
                    FieldName.PAYMENT_DATE: "2026-02-01",
                    FieldName.PAYMENT_AMOUNT: 1000,
                    FieldName.FAMILY_MEMBER_NAME: "山田 太郎",
                },
                status="tentative",
            )
            repo.save_receipt_result(
                receipt_id="R1",
                line_user_id="U1",
                line_message_id="m1",
                image_path="s3://bucket/raw/2026/02/01/r1.jpg",
                image_sha256="abc",
                result=_result(),
            )
            repo.save_receipt_result(
                receipt_id="R2",
                line_user_id="U2",
                line_message_id="m2",
                image_path="s3://bucket/raw/2026/02/01/r2.jpg",
                image_sha256="def",
                result=_result(),
            )

            paths = repo.purge_user_data("U1")
            self.assertEqual(paths, ["s3://bucket/raw/2026/02/01/r1.jpg"])
            self.assertEqual(repo.get_receipt_fields("R1"), {})
            self.assertTrue(repo.get_receipt_fields("R2"))
            self.assertIsNone(repo.get_active_session("U1"))
            self.assertEqual(repo.get_pending_count("U1"), 0)
            self.assertFalse(repo.list_family_members("U1"))
            self.assertFalse(repo.is_family_registration_completed("U1"))

    def test_record_and_get_field_correction_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            repo.record_field_correction("U1", FieldName.FAMILY_MEMBER_NAME, "クリニックA", "山田 花子")
            self.assertIsNone(
                repo.get_field_correction_hint("U1", FieldName.FAMILY_MEMBER_NAME, "クリニックA", min_count=2)
            )
            repo.record_field_correction("U1", FieldName.FAMILY_MEMBER_NAME, "クリニックA", "山田 花子")
            self.assertEqual(
                repo.get_field_correction_hint("U1", FieldName.FAMILY_MEMBER_NAME, "クリニックA", min_count=2),
                "山田 花子",
            )

    def test_find_potential_duplicates_and_delete_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            result = _result()
            repo.save_receipt_result(
                receipt_id="R1",
                line_user_id="U1",
                line_message_id="m1",
                image_path="s3://bucket/raw/r1.jpg",
                image_sha256="sha-1",
                result=result,
            )
            fields = repo.get_receipt_fields("R1")
            duplicate_key = "2026-02-01|テスト医院|山田太郎|1000"
            dupes_by_hash = repo.find_potential_duplicates("U1", "sha-1", None, limit=3)
            self.assertEqual(len(dupes_by_hash), 1)
            dupes_by_key = repo.find_potential_duplicates("U1", "other", duplicate_key, limit=3)
            self.assertEqual(len(dupes_by_key), 1)
            self.assertEqual(fields.get(FieldName.PAYMENT_AMOUNT), 1000)

            image_path = repo.delete_receipt("U1", "R1")
            self.assertEqual(image_path, "s3://bucket/raw/r1.jpg")
            self.assertEqual(repo.get_receipt_fields("R1"), {})

    def test_get_latest_receipt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            result = _result()
            repo.save_receipt_result(
                receipt_id="R1",
                line_user_id="U1",
                line_message_id="m1",
                image_path="s3://bucket/raw/r1.jpg",
                image_sha256="sha-1",
                result=result,
            )
            repo.save_receipt_result(
                receipt_id="R2",
                line_user_id="U1",
                line_message_id="m2",
                image_path="s3://bucket/raw/r2.jpg",
                image_sha256="sha-2",
                result=result,
            )

            self.assertEqual(repo.get_latest_receipt_id("U1"), "R2")
            repo.delete_receipt("U1", "R2")
            self.assertEqual(repo.get_latest_receipt_id("U1"), "R1")
            self.assertIsNone(repo.get_latest_receipt_id("U2"))

    def test_consume_ocr_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            now = datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc)

            for _ in range(3):
                allowed, reason = repo.consume_ocr_quota(
                    line_user_id="U1",
                    now_utc=now,
                    user_per_minute_limit=3,
                    user_per_day_limit=10,
                    global_per_day_limit=20,
                )
                self.assertTrue(allowed)
                self.assertIsNone(reason)

            allowed, reason = repo.consume_ocr_quota(
                line_user_id="U1",
                now_utc=now,
                user_per_minute_limit=3,
                user_per_day_limit=10,
                global_per_day_limit=20,
            )
            self.assertFalse(allowed)
            self.assertEqual(reason, "user_minute")

            for minute in range(1, 10):
                allowed, reason = repo.consume_ocr_quota(
                    line_user_id="U1",
                    now_utc=datetime(2026, 2, 25, 12, minute, tzinfo=timezone.utc),
                    user_per_minute_limit=3,
                    user_per_day_limit=10,
                    global_per_day_limit=20,
                )
                if minute <= 7:
                    self.assertTrue(allowed)
                    self.assertIsNone(reason)
                else:
                    self.assertFalse(allowed)
                    self.assertEqual(reason, "user_day")
                    break

    def test_aggregate_summary_includes_yearless_month_day_service_date(self) -> None:
        year = datetime.now(timezone.utc).year
        with tempfile.TemporaryDirectory() as tmp:
            repo = InboxRepository(str(Path(tmp) / "linebot.db"))
            repo.upsert_aggregate_entry(
                receipt_id="R1",
                line_user_id="U1",
                fields={
                    FieldName.PAYMENT_DATE: "02-17",
                    FieldName.PAYMENT_AMOUNT: "11,860円",
                    FieldName.PAYER_FACILITY_NAME: "外来センター病院",
                    FieldName.FAMILY_MEMBER_NAME: "山田 太郎",
                },
                status="confirmed",
            )
            total_y, count_y = repo.get_year_summary("U1", year)
            total_m, count_m = repo.get_month_summary("U1", year, 2)
            self.assertEqual((total_y, count_y), (11860, 1))
            self.assertEqual((total_m, count_m), (11860, 1))


def _result() -> ExtractionResult:
    return ExtractionResult(
        document_id="doc",
        household_id=None,
        document_type=DocumentType.PHARMACY,
        template_match=TemplateMatch(matched=False, template_family_id=None, score=0.0),
        fields={
            FieldName.PAYER_FACILITY_NAME: Candidate(
                field=FieldName.PAYER_FACILITY_NAME,
                value_raw="テスト医院",
                value_normalized="テスト医院",
                source_line_indices=[0],
                bbox=None,
                score=1.0,
                ocr_confidence=0.9,
                reasons=["test"],
            ),
            FieldName.PAYMENT_DATE: Candidate(
                field=FieldName.PAYMENT_DATE,
                value_raw="2026-02-01",
                value_normalized="2026-02-01",
                source_line_indices=[0],
                bbox=None,
                score=1.0,
                ocr_confidence=0.9,
                reasons=["test"],
            ),
            FieldName.PAYMENT_AMOUNT: Candidate(
                field=FieldName.PAYMENT_AMOUNT,
                value_raw="1000",
                value_normalized=1000,
                source_line_indices=[0],
                bbox=None,
                score=1.0,
                ocr_confidence=0.9,
                reasons=["test"],
            ),
            FieldName.FAMILY_MEMBER_NAME: Candidate(
                field=FieldName.FAMILY_MEMBER_NAME,
                value_raw="山田 太郎",
                value_normalized="山田 太郎",
                source_line_indices=[0],
                bbox=None,
                score=1.0,
                ocr_confidence=0.9,
                reasons=["test"],
            ),
        },
        decision=Decision(status=DecisionStatus.REVIEW_REQUIRED, confidence=0.5, reasons=["test"]),
        audit=AuditInfo(engine="test", engine_version="1", pipeline_version="1"),
        candidate_pool={},
        ocr_lines=[],
    )


if __name__ == "__main__":
    unittest.main()

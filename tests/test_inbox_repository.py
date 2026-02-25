from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.enums import FieldName
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


if __name__ == "__main__":
    unittest.main()


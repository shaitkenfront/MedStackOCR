from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.enums import FieldName
from inbox.repository import InboxRepository
from ledger.checks import (
    CODE_DUPLICATE,
    CODE_INCOMPLETE_DATE,
    CODE_MISSING_AMOUNT,
    CODE_UNCONFIRMED,
    CODE_UNKNOWN_FAMILY,
)
from ledger.models import CATEGORY_MEDICINE, CATEGORY_OTHER, CATEGORY_TREATMENT, SOURCE_MANUAL, SOURCE_OCR
from ledger.service import LedgerEntryNotFoundError, LedgerService
from ledger.validation import LedgerValidationError

try:
    import boto3

    try:
        from moto import mock_aws  # moto 5 以降
    except ImportError:
        from moto import mock_dynamodb as mock_aws  # moto 4
except Exception:  # pragma: no cover - moto が無い環境では DynamoDB 版のテストを飛ばす
    boto3 = None
    mock_aws = None


OCR_FIELDS = {
    FieldName.PAYMENT_DATE: "2026-03-10",
    FieldName.PAYMENT_AMOUNT: 3200,
    FieldName.PAYER_FACILITY_NAME: "さくら薬局",
    FieldName.FAMILY_MEMBER_NAME: "山田 太郎",
}

TRANSPORT_INPUT = {
    "service_date": "2026/4/2",
    "provider_name": "さくら病院 通院バス代",
    "family_member_name": "山田 花子",
    "amount_yen": "460",
}


class LedgerServiceContract:
    """SQLite 版と DynamoDB 版のリポジトリで同じ振る舞いになることを確かめる共通テスト。"""

    repo: Any

    def add_ocr_receipt(self, receipt_id: str, line_user_id: str, image_path: str) -> None:
        raise NotImplementedError

    def make_service(self) -> LedgerService:
        return LedgerService(self.repo)

    def add_ocr_entry(self, receipt_id: str = "R1", status: str = "confirmed", **overrides: Any) -> None:
        fields = dict(OCR_FIELDS)
        fields.update(overrides)
        self.add_ocr_receipt(receipt_id, "U1", f"s3://bucket/{receipt_id}.jpg")
        self.repo.upsert_aggregate_entry(receipt_id=receipt_id, line_user_id="U1", fields=fields, status=status)

    def test_ocr_entry_gets_default_category_and_source(self) -> None:
        self.add_ocr_entry()
        entries = self.make_service().list_entries("U1", 2026)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.source, SOURCE_OCR)
        self.assertEqual(entry.medical_category, CATEGORY_MEDICINE)
        self.assertEqual(entry.amount_yen, 3200)
        self.assertEqual(entry.reimbursed_yen, 0)

    def test_create_manual_transport_entry(self) -> None:
        service = self.make_service()
        entry = service.create_manual_entry("U1", TRANSPORT_INPUT)
        self.assertTrue(entry.entry_id.startswith("manual-"))
        self.assertEqual(entry.source, SOURCE_MANUAL)
        self.assertEqual(entry.service_date, "2026-04-02")
        self.assertEqual(entry.amount_yen, 460)
        self.assertEqual(entry.medical_category, CATEGORY_OTHER)
        self.assertEqual(entry.status, "confirmed")
        listed = service.list_entries("U1", 2026)
        self.assertEqual([item.entry_id for item in listed], [entry.entry_id])
        self.assertEqual(service.list_entries("U1", 2025), [])

    def test_create_manual_entry_requires_fields(self) -> None:
        with self.assertRaises(LedgerValidationError) as ctx:
            self.make_service().create_manual_entry("U1", {"service_date": "2026-04-02"})
        self.assertEqual(
            set(ctx.exception.errors),
            {"provider_name", "family_member_name", "amount_yen"},
        )

    def test_update_changes_date_and_keeps_single_row(self) -> None:
        self.add_ocr_entry()
        service = self.make_service()
        updated = service.update_entry(
            "U1",
            "R1",
            {"service_date": "2026-03-11", "reimbursed_yen": 1000, "medical_category": CATEGORY_TREATMENT, "note": "高額療養費"},
        )
        self.assertEqual(updated.service_date, "2026-03-11")
        self.assertEqual(updated.reimbursed_yen, 1000)
        self.assertEqual(len(self.repo.list_ledger_rows("U1")), 1)
        # OCR 側の項目値にも反映される
        self.assertEqual(self.repo.get_receipt_fields("R1")[FieldName.PAYMENT_DATE], "2026-03-11")

    def test_ocr_reconfirm_keeps_ledger_only_values(self) -> None:
        self.add_ocr_entry(status="tentative")
        service = self.make_service()
        service.update_entry("U1", "R1", {"reimbursed_yen": 500, "note": "保険金あり"})
        self.repo.upsert_aggregate_entry(receipt_id="R1", line_user_id="U1", fields=OCR_FIELDS, status="confirmed")
        entry = service.get_entry("U1", "R1")
        self.assertEqual(entry.status, "confirmed")
        self.assertEqual(entry.reimbursed_yen, 500)
        self.assertEqual(entry.note, "保険金あり")

    def test_update_rejects_reimbursed_over_amount(self) -> None:
        self.add_ocr_entry()
        with self.assertRaises(LedgerValidationError) as ctx:
            self.make_service().update_entry("U1", "R1", {"reimbursed_yen": 5000})
        self.assertIn("reimbursed_yen", ctx.exception.errors)

    def test_other_users_entry_is_not_found(self) -> None:
        self.add_ocr_entry()
        service = self.make_service()
        with self.assertRaises(LedgerEntryNotFoundError):
            service.update_entry("U2", "R1", {"amount_yen": 1})
        with self.assertRaises(LedgerEntryNotFoundError):
            service.delete_entry("U2", "R1")
        self.assertEqual(service.get_entry("U1", "R1").amount_yen, 3200)

    def test_delete_ocr_entry_returns_image_path(self) -> None:
        self.add_ocr_entry()
        result = self.make_service().delete_entry("U1", "R1")
        self.assertTrue(result.deleted)
        self.assertEqual(result.image_path, "s3://bucket/R1.jpg")
        self.assertEqual(self.repo.list_ledger_rows("U1"), [])

    def test_delete_manual_entry(self) -> None:
        service = self.make_service()
        entry = service.create_manual_entry("U1", TRANSPORT_INPUT)
        result = service.delete_entry("U1", entry.entry_id)
        self.assertTrue(result.deleted)
        self.assertIsNone(result.image_path)
        self.assertEqual(service.list_entries("U1", 2026), [])

    def test_check_reports_issues_and_summary(self) -> None:
        self.repo.upsert_family_member("U1", "山田 太郎", [])
        self.repo.upsert_family_member("U1", "山田 花子", [])
        self.add_ocr_entry("R1")
        self.add_ocr_entry("R2")  # 二重登録
        self.add_ocr_entry("R3", status="hold", **{FieldName.PAYMENT_AMOUNT: None, FieldName.FAMILY_MEMBER_NAME: "山田 次郎"})
        service = self.make_service()
        service.create_manual_entry("U1", TRANSPORT_INPUT)

        report = service.check("U1", 2026)
        codes = {(issue.entry_id, issue.code) for issue in report.issues}
        self.assertIn(("R1", CODE_DUPLICATE), codes)
        self.assertIn(("R2", CODE_DUPLICATE), codes)
        self.assertIn(("R3", CODE_MISSING_AMOUNT), codes)
        self.assertIn(("R3", CODE_UNCONFIRMED), codes)
        self.assertIn(("R3", CODE_UNKNOWN_FAMILY), codes)
        self.assertFalse(report.ready_for_export)
        self.assertEqual(report.summary.entry_count, 4)
        self.assertEqual(report.summary.total_amount_yen, 3200 * 2 + 460)
        self.assertEqual(report.summary.unconfirmed_count, 1)
        self.assertEqual(report.issues[0].severity, "error")


class SqliteLedgerServiceTest(LedgerServiceContract, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = InboxRepository(str(Path(self._tmp.name) / "linebot.db"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add_ocr_receipt(self, receipt_id: str, line_user_id: str, image_path: str) -> None:
        with self.repo._connect() as conn:
            conn.execute(
                """
                INSERT INTO receipts(receipt_id, line_user_id, line_message_id, image_path, image_sha256,
                                     decision_status, decision_confidence, created_at, updated_at)
                VALUES(?, ?, 'M1', ?, 'sha', 'AUTO_ACCEPT', 0.9, '2026-03-10T00:00:00+00:00', '2026-03-10T00:00:00+00:00')
                """,
                (receipt_id, line_user_id, image_path),
            )
            conn.commit()


@unittest.skipIf(mock_aws is None, "moto is not installed")
class DynamoLedgerServiceTest(LedgerServiceContract, unittest.TestCase):
    def setUp(self) -> None:
        from inbox.dynamo_repository import DynamoInboxRepository

        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        self._mock = mock_aws()
        self._mock.start()
        ddb = boto3.resource("dynamodb", region_name="ap-northeast-1")
        _create_tables(ddb)
        self.ddb = ddb
        self.repo = DynamoInboxRepository(table_prefix="t", dynamodb_resource=ddb)

    def tearDown(self) -> None:
        self._mock.stop()

    def add_ocr_receipt(self, receipt_id: str, line_user_id: str, image_path: str) -> None:
        self.ddb.Table("t-receipts").put_item(
            Item={
                "receipt_id": receipt_id,
                "line_user_id": line_user_id,
                "image_path": image_path,
                "created_at": "2026-03-10T00:00:00+00:00",
            }
        )


def _create_tables(ddb: Any) -> None:
    def string_attrs(*names: str) -> list[dict[str, str]]:
        return [{"AttributeName": name, "AttributeType": "S"} for name in names]

    def key_schema(pk: str, sk: str | None = None) -> list[dict[str, str]]:
        schema = [{"AttributeName": pk, "KeyType": "HASH"}]
        if sk:
            schema.append({"AttributeName": sk, "KeyType": "RANGE"})
        return schema

    def gsi(name: str, pk: str, sk: str | None = None) -> dict[str, Any]:
        return {"IndexName": name, "KeySchema": key_schema(pk, sk), "Projection": {"ProjectionType": "ALL"}}

    tables = [
        ("t-receipts", key_schema("receipt_id"), string_attrs("receipt_id", "line_user_id", "created_at"),
         [gsi("gsi_user_created", "line_user_id", "created_at")]),
        ("t-receipt-fields", key_schema("receipt_id", "field_name"), string_attrs("receipt_id", "field_name"), []),
        ("t-sessions", key_schema("session_id"), string_attrs("session_id", "line_user_id", "updated_at"),
         [gsi("line_user_id_updated_at_index", "line_user_id", "updated_at")]),
        ("t-aggregate-entries", key_schema("line_user_id", "service_date_receipt"),
         string_attrs("line_user_id", "service_date_receipt", "receipt_id"), [gsi("receipt_id_index", "receipt_id")]),
        ("t-family-registry", key_schema("line_user_id", "record_type"), string_attrs("line_user_id", "record_type"), []),
    ]
    for name, keys, attrs, indexes in tables:
        kwargs: dict[str, Any] = {
            "TableName": name,
            "KeySchema": keys,
            "AttributeDefinitions": attrs,
            "BillingMode": "PAY_PER_REQUEST",
        }
        if indexes:
            kwargs["GlobalSecondaryIndexes"] = indexes
        ddb.create_table(**kwargs)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from core.enums import FieldName
from inbox.aggregate_service import AggregateService
from inbox.repository import InboxRepository
from ledger.api import JST, ApiRequest, LedgerApi
from ledger.auth import LineIdTokenVerifier
from ledger.checks import CODE_FORM_TEXT_TOO_LONG
from ledger.models import CATEGORY_MEDICINE, CATEGORY_OTHER, CATEGORY_TREATMENT, LedgerEntry
from ledger.service import LedgerService
from ledger.tax_form import MAX_ROWS, TEMPLATE_PATH, FormRow, TaxFormError, plan_form_rows, render_form

TEMPLATE_SHA256 = "b69cadc0d073e5e56bee6a2ef6daaf82138ced7468780f1269dec681dffee842"
CHANGED_PARTS = {"xl/worksheets/sheet1.xml", "xl/sharedStrings.xml", "xl/workbook.xml"}


def entry(entry_id: str, **overrides: object) -> LedgerEntry:
    values: dict[str, object] = {
        "entry_id": entry_id,
        "line_user_id": "U1",
        "source": "ocr",
        "status": "confirmed",
        "service_date": "2026-03-10",
        "provider_name": "さくら薬局",
        "family_member_name": "山田 太郎",
        "amount_yen": 1000,
        "reimbursed_yen": 0,
        "medical_category": CATEGORY_MEDICINE,
        "note": None,
        "created_at": "2026-03-10T00:00:00+00:00",
        "updated_at": "2026-03-10T00:00:00+00:00",
    }
    values.update(overrides)
    return LedgerEntry(**values)  # type: ignore[arg-type]


def read_sheet(content: bytes) -> dict[str, object]:
    """書き出したフォームの入力欄を、共有文字列を解決して {セル: 値} にする。"""
    with zipfile.ZipFile(io.BytesIO(content)) as book:
        shared = re.findall(r"<si>(.*?)</si>", book.read("xl/sharedStrings.xml").decode("utf-8"), re.S)
        texts = ["".join(re.findall(r"<t[^>]*>(.*?)</t>", item, re.S)) for item in shared]
        sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
    cells: dict[str, object] = {}
    for ref, attrs, value in re.findall(r'<c r="([B-J]\d+)"([^>]*)><v>([^<]*)</v></c>', sheet):
        cells[ref] = texts[int(value)] if 't="s"' in attrs else int(value)
    return cells


class TemplateTest(unittest.TestCase):
    def test_bundled_template_is_the_official_file(self) -> None:
        self.assertEqual(hashlib.sha256(TEMPLATE_PATH.read_bytes()).hexdigest(), TEMPLATE_SHA256)


class PlanFormRowsTest(unittest.TestCase):
    def test_rows_are_sorted_and_dates_formatted(self) -> None:
        plan = plan_form_rows([entry("B", service_date="2026-05-01"), entry("A", service_date="2026-02-01")])
        self.assertEqual([row.entry_ids for row in plan.rows], [["A"], ["B"]])
        self.assertEqual(plan.rows[0].payment_date, "2026/02/01")
        self.assertFalse(plan.aggregated)

    def test_long_names_are_truncated_with_notice(self) -> None:
        plan = plan_form_rows([entry("A", provider_name="あ" * 25, family_member_name="い" * 12)])
        self.assertEqual(plan.rows[0].provider_name, "あ" * 20)
        self.assertEqual(plan.rows[0].family_member_name, "い" * 10)
        self.assertEqual({notice.code for notice in plan.notices}, {"provider_name_truncated", "family_name_truncated"})

    def test_half_width_katakana_is_normalized(self) -> None:
        plan = plan_form_rows([entry("A", provider_name="ｻｸﾗ薬局")])
        self.assertEqual(plan.rows[0].provider_name, "サクラ薬局")

    def test_more_than_995_entries_are_aggregated(self) -> None:
        entries = [entry(f"E{i}", service_date=f"2026-{1 + i % 12:02d}-01", amount_yen=100) for i in range(MAX_ROWS + 5)]
        entries.append(entry("X", provider_name="駅前クリニック", medical_category=CATEGORY_TREATMENT, amount_yen=5000))
        plan = plan_form_rows(entries)
        self.assertTrue(plan.aggregated)
        self.assertEqual(len(plan.rows), 2)
        self.assertEqual(plan.total_amount_yen, 100 * (MAX_ROWS + 5) + 5000)
        self.assertTrue(all(row.payment_date is None for row in plan.rows))
        self.assertIn("aggregated", {notice.code for notice in plan.notices})


class RenderFormTest(unittest.TestCase):
    def test_writes_cells_and_keeps_other_parts(self) -> None:
        rows = [
            FormRow("山田 太郎", "さくら薬局", CATEGORY_MEDICINE, 3200, 0, "2026/03/10", ["A"]),
            FormRow("山田 花子", "JR & <バス>", CATEGORY_OTHER, 460, 0, None, ["B"]),
            FormRow("山田 花子", "東京医科大学病院", CATEGORY_TREATMENT, 250000, 200000, "2026/04/02", ["C"]),
        ]
        content = render_form(rows)
        cells = read_sheet(content)
        self.assertEqual(cells["B9"], "山田 太郎")
        self.assertEqual(cells["C9"], "さくら薬局")
        self.assertEqual(cells["E9"], "該当する")
        self.assertEqual(cells["H9"], 3200)
        self.assertEqual(cells["J9"], "2026/03/10")
        self.assertEqual(cells["C10"], "JR &amp; &lt;バス&gt;")  # XML として正しくエスケープされている
        self.assertEqual(cells["G10"], "該当する")
        self.assertNotIn("J10", cells)
        self.assertEqual((cells["D11"], cells["H11"], cells["I11"]), ("該当する", 250000, 200000))
        self.assertNotIn("B12", cells)

        with zipfile.ZipFile(io.BytesIO(content)) as result, zipfile.ZipFile(TEMPLATE_PATH) as template:
            self.assertIsNone(result.testzip())
            self.assertEqual([i.filename for i in result.infolist()], [i.filename for i in template.infolist()])
            for info in template.infolist():
                if info.filename not in CHANGED_PARTS:
                    self.assertEqual(result.read(info.filename), template.read(info.filename), info.filename)
            sheet = result.read("xl/worksheets/sheet1.xml").decode("utf-8")
            template_sheet = template.read("xl/worksheets/sheet1.xml").decode("utf-8")
            # 入力規則・シートの保護は元のまま
            for tag in ("<dataValidations", "<sheetProtection"):
                self.assertEqual(sheet[sheet.index(tag):], template_sheet[template_sheet.index(tag):])
            self.assertIn('fullCalcOnLoad="1"', result.read("xl/workbook.xml").decode("utf-8"))
            shared = result.read("xl/sharedStrings.xml").decode("utf-8")
            unique = int(re.search(r'uniqueCount="(\d+)"', shared).group(1))  # type: ignore[union-attr]
            self.assertEqual(unique, len(re.findall(r"<si>", shared)))

    def test_too_many_rows_is_rejected(self) -> None:
        rows = [FormRow("a", "b", CATEGORY_OTHER, 1, 0, None, []) for _ in range(MAX_ROWS + 1)]
        with self.assertRaises(TaxFormError):
            render_form(rows)


class TaxFormServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = InboxRepository(str(Path(self._tmp.name) / "linebot.db"))
        self.service = LedgerService(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add_ocr(self, receipt_id: str, status: str, amount: int | None = 1000) -> None:
        self.repo.upsert_aggregate_entry(
            receipt_id=receipt_id,
            line_user_id="U1",
            fields={
                FieldName.PAYMENT_DATE: "2026-03-10",
                FieldName.PAYMENT_AMOUNT: amount,
                FieldName.PAYER_FACILITY_NAME: "さくら薬局",
                FieldName.FAMILY_MEMBER_NAME: "山田 太郎",
            },
            status=status,
        )

    def test_only_confirmed_entries_without_errors_are_exported(self) -> None:
        self.add_ocr("R1", "confirmed")
        self.add_ocr("R2", "tentative")
        self.add_ocr("R3", "confirmed", amount=None)
        manual = self.service.create_manual_entry(
            "U1",
            {"service_date": "2026-04-02", "provider_name": "通院バス", "family_member_name": "山田 花子", "amount_yen": 460},
        )
        export = self.service.prepare_tax_form("U1", 2026, render=True)
        self.assertEqual(export.included_count, 2)
        self.assertEqual(export.total_amount_yen, 1460)
        self.assertEqual(
            sorted((item["entry_id"], item["reason"]) for item in export.excluded),
            [("R2", "unconfirmed"), ("R3", "has_error")],
        )
        assert export.content is not None
        cells = read_sheet(export.content)
        self.assertEqual({cells["C9"], cells["C10"]}, {"さくら薬局", "通院バス"})
        self.assertEqual(cells["G10"] if cells["C10"] == "通院バス" else cells["G9"], "該当する")
        self.assertEqual(export.filename, "iryouhi_shukei_2026.xlsx")
        self.assertIsNotNone(manual)

    def test_preview_does_not_render(self) -> None:
        self.add_ocr("R1", "confirmed")
        self.assertIsNone(self.service.prepare_tax_form("U1", 2026).content)

    def test_check_warns_long_names(self) -> None:
        self.service.create_manual_entry(
            "U1",
            {"service_date": "2026-04-02", "provider_name": "あ" * 21, "family_member_name": "山田 花子", "amount_yen": 460},
        )
        codes = {issue.code for issue in self.service.check("U1", 2026).issues}
        self.assertIn(CODE_FORM_TEXT_TOO_LONG, codes)

    def test_api_preview_and_create(self) -> None:
        self.add_ocr("R1", "confirmed")
        self.add_ocr("R2", "hold")

        def line_verify(url: str, form: dict[str, str]) -> tuple[int, str]:
            return 200, json.dumps({"sub": "U1", "aud": "1", "exp": 2_000_000_000})

        uploads: list[tuple[str, str, bytes]] = []

        def uploader(user_id: str, filename: str, content: bytes) -> str:
            uploads.append((user_id, filename, content))
            return "https://signed.example/form.xlsx"

        api = LedgerApi(
            self.service,
            LineIdTokenVerifier("1", http_post=line_verify, clock=lambda: 1_900_000_000),
            export_uploader=uploader,
            now=lambda: datetime(2026, 11, 2, tzinfo=JST),
        )
        headers = {"x-line-id-token": "t"}
        preview = api.handle(ApiRequest(method="GET", path="/api/export", query={"year": "2026"}, headers=headers))
        self.assertEqual(preview.status, 200)
        self.assertEqual((preview.body["included_count"], preview.body["excluded_counts"]), (1, {"unconfirmed": 1}))
        self.assertEqual(uploads, [])

        created = api.handle(ApiRequest(method="POST", path="/api/export", headers=headers, body=json.dumps({"year": 2026})))
        self.assertEqual(created.status, 201)
        self.assertEqual(created.body["url"], "https://signed.example/form.xlsx")
        self.assertEqual(uploads[0][:2], ("U1", "iryouhi_shukei_2026.xlsx"))

        empty = api.handle(ApiRequest(method="POST", path="/api/export", headers=headers, body=json.dumps({"year": 2025})))
        self.assertEqual((empty.status, empty.body["error"]), (400, "nothing_to_export"))


class ExportUploaderTest(unittest.TestCase):
    def test_upload_goes_to_exports_prefix_with_attachment(self) -> None:
        from app.lambda_handlers.api_handler import build_api_from_env

        s3 = mock.Mock()
        s3.generate_presigned_url.return_value = "https://signed"
        api = build_api_from_env({"RECEIPT_BUCKET": "receipts", "LINE_LOGIN_CHANNEL_ID": "1"}, dynamodb_resource=mock.Mock(), s3_client=s3)
        assert api.export_uploader is not None
        self.assertEqual(api.export_uploader("U1", "iryouhi_shukei_2026.xlsx", b"x"), "https://signed")
        put = s3.put_object.call_args.kwargs
        self.assertEqual(put["Bucket"], "receipts")
        self.assertTrue(put["Key"].startswith("exports/"))
        self.assertTrue(put["Key"].endswith("/iryouhi_shukei_2026.xlsx"))
        self.assertNotIn("U1", put["Key"])  # 利用者IDはそのまま使わない
        params = s3.generate_presigned_url.call_args.kwargs["Params"]
        self.assertIn("attachment", params["ResponseContentDisposition"])
        self.assertEqual(s3.generate_presigned_url.call_args.kwargs["ExpiresIn"], 600)


class LineCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = InboxRepository(str(Path(self._tmp.name) / "linebot.db"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tax_form_command_links_to_export_view(self) -> None:
        service = AggregateService(self.repo, liff_id="2012319275-test")
        with mock.patch("inbox.aggregate_service.datetime") as fake:
            fake.now.return_value = datetime(2027, 2, 10, tzinfo=JST)
            messages = service.handle_text_command("U1", "申告資料")
        assert messages is not None
        self.assertIn("2026年分", messages[0]["text"])
        uri = messages[1]["template"]["actions"][0]["uri"]
        self.assertEqual(uri, "https://liff.line.me/2012319275-test?view=export&year=2026")

    def test_ledger_command_and_missing_liff(self) -> None:
        messages = AggregateService(self.repo, liff_id="L").handle_text_command("U1", "明細")
        assert messages is not None
        self.assertEqual(messages[0]["template"]["actions"][0]["uri"], "https://liff.line.me/L")
        unavailable = AggregateService(self.repo).handle_text_command("U1", "申告資料")
        assert unavailable is not None
        self.assertIn("準備中", unavailable[0]["text"])


if __name__ == "__main__":
    unittest.main()

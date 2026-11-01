from __future__ import annotations

import json
import unittest
from unittest import mock

from core.enums import DecisionStatus, DocumentType, FieldName
from core.models import AuditInfo, Candidate, Decision, ExtractionResult, OCRLine, TemplateMatch
from extractors.facility_extractor import FacilityExtractor
from inbox.ocr_archive import archive_path_for, build_ocr_archive, lines_from_archive

CANDIDATE_THRESHOLD = 2.5  # app/config.py の pipeline.candidate_threshold


def receipt(*rows: tuple[str, float] | tuple[str, float, float]) -> list[OCRLine]:
    """(テキスト, 縦位置[, 文字の高さ]) の並びから OCR の行を作る。"""
    lines: list[OCRLine] = []
    for index, row in enumerate(rows):
        text, y = row[0], row[1]
        height = row[2] if len(row) > 2 else 0.03
        lines.append(
            OCRLine(
                text=text,
                bbox=(0.1, y - height / 2, 0.8, y + height / 2),
                polygon=None,
                confidence=0.95,
                line_index=index,
                page=1,
            )
        )
    return lines


def top_payer(document_type: DocumentType, lines: list[OCRLine], known: list[str] | None = None) -> str | None:
    candidates = FacilityExtractor().extract(document_type, lines, known_names=known)[FieldName.PAYER_FACILITY_NAME]
    if not candidates or candidates[0].score < CANDIDATE_THRESHOLD:
        return None
    return str(candidates[0].value_normalized)


class FacilityHeuristicsTest(unittest.TestCase):
    def test_greetings_and_titles_are_not_names(self) -> None:
        lines = receipt(
            ("毎度ありがとうございます", 0.05),
            ("ご利用明細", 0.09),
            ("お薬のことなら何でもご相談ください", 0.12),
            ("2026年3月10日 10:32", 0.16),
            ("合計 1,200円", 0.5),
        )
        self.assertIsNone(top_payer(DocumentType.PHARMACY, lines))

    def test_title_and_notice_with_facility_word_lose_to_real_name(self) -> None:
        lines = receipt(
            ("調剤明細", 0.05),
            ("薬局からのお知らせ", 0.1),
            ("マツモトキヨシ 新宿東口店", 0.82, 0.05),
            ("東京都新宿区新宿3-1-1", 0.87),
        )
        self.assertEqual(top_payer(DocumentType.PHARMACY, lines), "マツモトキヨシ 新宿東口店")

    def test_drugstore_without_pharmacy_keyword_found_by_layout(self) -> None:
        lines = receipt(
            ("ご来店ありがとうございます", 0.05),
            ("マツモトキヨシ 新宿東口店", 0.82, 0.05),
            ("東京都新宿区新宿3-1-1", 0.87),
            ("TEL 03-1234-5678", 0.9),
        )
        self.assertEqual(top_payer(DocumentType.PHARMACY, lines), "マツモトキヨシ 新宿東口店")

    def test_address_and_designation_are_skipped(self) -> None:
        lines = receipt(
            ("健康サポート薬局", 0.05),
            ("東京都千代田区丸の内1丁目", 0.08),
            ("さくら薬局 駅前店", 0.12, 0.045),
            ("TEL 03-0000-0000", 0.16),
        )
        self.assertEqual(top_payer(DocumentType.PHARMACY, lines), "さくら薬局 駅前店")

    def test_prescribing_clinic_is_not_payer_on_pharmacy_receipt(self) -> None:
        lines = receipt(
            ("処方箋交付医療機関", 0.3),
            ("山田内科クリニック", 0.33),
            ("ひまわり薬局", 0.85, 0.045),
            ("〒100-0001 東京都千代田区", 0.89),
        )
        candidates = FacilityExtractor().extract(DocumentType.PHARMACY, lines)
        self.assertEqual(candidates[FieldName.PAYER_FACILITY_NAME][0].value_normalized, "ひまわり薬局")
        self.assertEqual(candidates[FieldName.PRESCRIBING_FACILITY_NAME][0].value_normalized, "山田内科クリニック")

    def test_corporate_name_on_separate_line(self) -> None:
        lines = receipt(
            ("医療法人社団 健康会", 0.8),
            ("山田内科クリニック", 0.84),
            ("TEL 03-1111-2222", 0.88),
        )
        self.assertEqual(top_payer(DocumentType.CLINIC_OR_HOSPITAL, lines), "山田内科クリニック")

    def test_contact_on_same_line_is_trimmed(self) -> None:
        lines = receipt(("あおば歯科医院 TEL 045-123-4567", 0.1))
        self.assertEqual(top_payer(DocumentType.CLINIC_OR_HOSPITAL, lines), "あおば歯科医院")

    def test_insurance_pharmacy_prefix_is_removed(self) -> None:
        lines = receipt(("保険薬局 ひまわり薬局", 0.1), ("TEL 03-1234-5678", 0.14))
        self.assertEqual(top_payer(DocumentType.PHARMACY, lines), "ひまわり薬局")

    def test_public_hospital_name_is_not_treated_as_address(self) -> None:
        lines = receipt(("東京都立駒込病院", 0.08, 0.05), ("会計窓口", 0.3))
        self.assertEqual(top_payer(DocumentType.CLINIC_OR_HOSPITAL, lines), "東京都立駒込病院")


class TwoLineNameTest(unittest.TestCase):
    def test_name_split_over_two_lines_is_joined(self) -> None:
        lines = receipt(
            ("東京医科大学", 0.78, 0.04),
            ("八王子医療センター", 0.82, 0.04),
            ("TEL 042-000-0000", 0.86),
            ("初診料", 0.3),
        )
        self.assertEqual(top_payer(DocumentType.CLINIC_OR_HOSPITAL, lines), "東京医科大学 八王子医療センター")

    def test_red_cross_prefix_is_joined(self) -> None:
        lines = receipt(("日本赤十字社", 0.1), ("さいたま赤十字病院", 0.14), ("〒330-0000", 0.18))
        self.assertEqual(top_payer(DocumentType.CLINIC_OR_HOSPITAL, lines), "日本赤十字社 さいたま赤十字病院")

    def test_unrelated_line_above_is_not_joined(self) -> None:
        lines = receipt(("お会計窓口", 0.1), ("あおば歯科医院", 0.14), ("TEL 045-000-0000", 0.18))
        self.assertEqual(top_payer(DocumentType.CLINIC_OR_HOSPITAL, lines), "あおば歯科医院")

    def test_far_line_above_is_not_joined(self) -> None:
        lines = receipt(("東京医科大学", 0.5), ("八王子医療センター", 0.82), ("TEL 042-000-0000", 0.86))
        self.assertEqual(top_payer(DocumentType.CLINIC_OR_HOSPITAL, lines), "八王子医療センター")


class KnownProviderTest(unittest.TestCase):
    def test_misread_name_is_replaced_by_known_name(self) -> None:
        lines = receipt(("お会計", 0.05), ("さくら薬局駅前庖", 0.85), ("TEL 03-0000-0000", 0.89))
        self.assertEqual(top_payer(DocumentType.PHARMACY, lines, ["さくら薬局 駅前店"]), "さくら薬局 駅前店")

    def test_known_name_beats_larger_other_line(self) -> None:
        lines = receipt(
            ("ウエルシア", 0.05, 0.08),  # ロゴの近くの大きな文字
            ("ウエルシア薬局 横浜店", 0.88),
            ("TEL 045-000-0000", 0.92),
        )
        self.assertEqual(top_payer(DocumentType.PHARMACY, lines, ["ウエルシア薬局 横浜店"]), "ウエルシア薬局 横浜店")

    def test_other_branch_is_not_replaced(self) -> None:
        lines = receipt(("さくら薬局 本町店", 0.1), ("TEL 03-0000-0000", 0.14))
        self.assertEqual(top_payer(DocumentType.PHARMACY, lines, ["さくら薬局 駅前店"]), "さくら薬局 本町店")

    def test_line_containing_known_name_uses_known_name(self) -> None:
        lines = receipt(("医療法人社団健康会 山田内科", 0.1))
        self.assertEqual(top_payer(DocumentType.CLINIC_OR_HOSPITAL, lines, ["山田内科"]), "山田内科")


class OcrArchiveTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        lines = receipt(("さくら薬局", 0.1), ("合計 1,200円", 0.5))
        result = ExtractionResult(
            document_id="doc",
            household_id=None,
            document_type=DocumentType.PHARMACY,
            template_match=TemplateMatch(matched=False, template_family_id=None, score=0.0),
            fields={
                FieldName.PAYER_FACILITY_NAME: Candidate(
                    field=FieldName.PAYER_FACILITY_NAME,
                    value_raw="さくら薬局",
                    value_normalized="さくら薬局",
                    source_line_indices=[0],
                    bbox=None,
                    score=5.0,
                    ocr_confidence=0.9,
                    reasons=[],
                ),
                FieldName.PAYMENT_AMOUNT: None,
            },
            decision=Decision(status=DecisionStatus.REVIEW_REQUIRED, confidence=0.7, reasons=[]),
            audit=AuditInfo(engine="documentai", engine_version="x", pipeline_version="0.1.0"),
            ocr_lines=lines,
        )
        data = json.loads(json.dumps(build_ocr_archive("R1", result), ensure_ascii=False))
        self.assertEqual(data["selected"], {FieldName.PAYER_FACILITY_NAME: "さくら薬局"})
        self.assertEqual(data["document_type"], "pharmacy")
        restored = lines_from_archive(data)
        self.assertEqual([line.text for line in restored], ["さくら薬局", "合計 1,200円"])
        self.assertEqual(restored[0].bbox, lines[0].bbox)
        self.assertEqual(archive_path_for("s3://b/raw/R1.jpg"), "s3://b/raw/R1.jpg.ocr.json")
        self.assertEqual(archive_path_for(""), "")

    def test_worker_saves_and_deletes_archive_next_to_image(self) -> None:
        from app.lambda_handlers.worker_handler import LineEventWorker

        worker = LineEventWorker.__new__(LineEventWorker)
        worker._s3_client = mock.Mock()
        worker.repository = mock.Mock()
        worker.repository.list_ledger_rows.return_value = [
            {"provider_name": "さくら薬局", "status": "confirmed"},
            {"provider_name": "さくら薬局", "status": "confirmed"},
            {"provider_name": "保留中の薬局", "status": "hold"},
        ]
        self.assertEqual(worker._known_provider_names("U1"), ["さくら薬局"])

        result = mock.Mock()
        with mock.patch("app.lambda_handlers.worker_handler.build_ocr_archive", return_value={"lines": []}):
            worker._persist_ocr_archive(image_path="s3://b/raw/R1.jpg", receipt_id="R1", result=result)
        put = worker._s3_client.put_object.call_args.kwargs
        self.assertEqual((put["Bucket"], put["Key"]), ("b", "raw/R1.jpg.ocr.json"))

        worker._delete_stored_image("s3://b/raw/R1.jpg")
        deleted = [call.kwargs["Key"] for call in worker._s3_client.delete_object.call_args_list]
        self.assertEqual(sorted(deleted), ["raw/R1.jpg", "raw/R1.jpg.ocr.json"])

    def test_archive_failure_does_not_raise(self) -> None:
        from app.lambda_handlers.worker_handler import LineEventWorker

        worker = LineEventWorker.__new__(LineEventWorker)
        worker._s3_client = mock.Mock()
        worker._s3_client.put_object.side_effect = RuntimeError("s3 down")
        with mock.patch("app.lambda_handlers.worker_handler.build_ocr_archive", return_value={"lines": []}):
            worker._persist_ocr_archive(image_path="s3://b/raw/R1.jpg", receipt_id="R1", result=mock.Mock())


if __name__ == "__main__":
    unittest.main()

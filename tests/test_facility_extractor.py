from __future__ import annotations

import unittest

from core.enums import DocumentType, FieldName
from core.models import OCRLine
from extractors.facility_extractor import FacilityExtractor


class FacilityExtractorTest(unittest.TestCase):
    def test_clinic_payer_prefers_facility_name_over_patient_info(self) -> None:
        extractor = FacilityExtractor()
        lines = [
            OCRLine(
                text="氏名 山田 太郎",
                bbox=(0.1, 0.18, 0.3, 0.22),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="負担割合 3割",
                bbox=(0.4, 0.22, 0.55, 0.26),
                polygon=None,
                confidence=0.95,
                line_index=1,
                page=1,
            ),
            OCRLine(
                text="医療法人 サンプルクリニック",
                bbox=(0.15, 0.84, 0.45, 0.9),
                polygon=None,
                confidence=0.95,
                line_index=2,
                page=1,
            ),
        ]
        candidates = extractor.extract(DocumentType.CLINIC_OR_HOSPITAL, lines)
        payer_candidates = candidates[FieldName.PAYER_FACILITY_NAME]
        self.assertTrue(payer_candidates)
        self.assertEqual(payer_candidates[0].value_normalized, "医療法人 サンプルクリニック")

    def test_pharmacy_payer_ignores_document_title(self) -> None:
        extractor = FacilityExtractor()
        lines = [
            OCRLine(
                text="調剤明細書",
                bbox=(0.3, 0.1, 0.45, 0.14),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="サンプル薬局 本店",
                bbox=(0.42, 0.18, 0.62, 0.22),
                polygon=None,
                confidence=0.95,
                line_index=1,
                page=1,
            ),
            OCRLine(
                text="TEL : 03-0000-0000",
                bbox=(0.42, 0.24, 0.62, 0.27),
                polygon=None,
                confidence=0.95,
                line_index=2,
                page=1,
            ),
        ]
        candidates = extractor.extract(DocumentType.PHARMACY, lines)
        payer_candidates = candidates[FieldName.PAYER_FACILITY_NAME]
        self.assertTrue(payer_candidates)
        self.assertEqual(payer_candidates[0].value_normalized, "サンプル薬局 本店")

    def test_clinic_payer_keeps_billing_source_label(self) -> None:
        extractor = FacilityExtractor()
        lines = [
            OCRLine(
                text="請求元:サンプルクリニック 本院",
                bbox=(0.42, 0.2, 0.62, 0.23),
                polygon=None,
                confidence=0.56,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="TEL: 03-0000-0001",
                bbox=(0.42, 0.23, 0.56, 0.25),
                polygon=None,
                confidence=0.85,
                line_index=1,
                page=1,
            ),
            OCRLine(
                text="請求金額 21,000円",
                bbox=(0.42, 0.26, 0.56, 0.28),
                polygon=None,
                confidence=0.9,
                line_index=2,
                page=1,
            ),
        ]
        candidates = extractor.extract(DocumentType.CLINIC_OR_HOSPITAL, lines)
        payer_candidates = candidates[FieldName.PAYER_FACILITY_NAME]
        self.assertTrue(payer_candidates)
        self.assertEqual(payer_candidates[0].value_normalized, "請求元:サンプルクリニック 本院")


if __name__ == "__main__":
    unittest.main()

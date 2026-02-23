from __future__ import annotations

import re

from core.enums import DocumentType
from core.models import OCRLine

PHARMACY_KEYWORDS = ("薬局", "調剤", "処方箋", "保険薬局", "ファーマシー")
CLINIC_KEYWORDS = ("病院", "医院", "クリニック", "診療所")


def _has_prescription_keyword(text: str) -> bool:
    return "処方箋" in text and "処方箋料" not in text


class DocumentClassifier:
    def classify(self, lines: list[OCRLine]) -> tuple[DocumentType, float, list[str], float]:
        if not lines:
            return DocumentType.UNKNOWN, 0.0, ["no_ocr_lines"], 0.0

        pharmacy_score = 0.0
        clinic_score = 0.0
        reasons: list[str] = []
        quality = sum(line.confidence for line in lines) / len(lines)

        for line in lines:
            text = line.text
            for kw in PHARMACY_KEYWORDS:
                if kw == "処方箋":
                    matched = _has_prescription_keyword(text)
                else:
                    matched = kw in text
                if matched:
                    pharmacy_score += 1.6
                    reasons.append(f"pharmacy_keyword:{kw}")
            for kw in CLINIC_KEYWORDS:
                if kw in text:
                    clinic_score += 1.2
                    reasons.append(f"clinic_keyword:{kw}")

            if re.search(r"処方箋交付|保険医療機関", text):
                pharmacy_score += 0.8
                reasons.append("pharmacy_context:prescription_block")

        diff = abs(pharmacy_score - clinic_score)
        if quality < 0.45:
            return DocumentType.UNKNOWN, 0.2, reasons + ["low_ocr_quality"], quality
        if pharmacy_score == 0 and clinic_score == 0:
            return DocumentType.UNKNOWN, 0.3, reasons + ["no_domain_keywords"], quality
        if diff < 1.0:
            return DocumentType.UNKNOWN, 0.4, reasons + ["score_gap_too_small"], quality

        if pharmacy_score > clinic_score:
            confidence = min(1.0, 0.55 + diff / 10.0)
            return DocumentType.PHARMACY, confidence, reasons, quality

        confidence = min(1.0, 0.55 + diff / 10.0)
        return DocumentType.CLINIC_OR_HOSPITAL, confidence, reasons, quality

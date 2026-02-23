from __future__ import annotations

import re

from core.enums import DocumentType, FieldName
from core.models import Candidate, OCRLine
from extractors.common import contains_any, count_digits, is_near_line, is_top_region, normalize_spaces

PHARMACY_KEYWORDS = ("薬局", "調剤", "ファーマシー", "保険薬局")
CLINIC_KEYWORDS = ("病院", "医院", "クリニック", "診療所")
PRESCRIBING_CONTEXT = ("処方箋", "保険医療機関", "交付", "医師")
CONTACT_ANCHORS = ("〒", "TEL", "領収書", "発行")
NON_NAME_HINTS = (
    "領収日",
    "発行日",
    "調剤日",
    "受診日",
    "診療日",
    "請求額",
    "請求金額",
    "請求書",
    "合計",
    "お支払",
    "税込",
    "税率",
    "点数",
    "負担割合",
    "保険種類",
    "保険適用",
    "氏名",
    "患者",
    "生年月日",
    "保険者番号",
    "記号番号",
    "明細書",
    "領収書",
    "領収証",
    "調剤明細書",
)
NON_NAME_EXACT = {"調剤", "明細", "領収", "合計", "内訳"}

RE_NAME_PREFIX = re.compile(
    r"^(処方箋交付医療機関|保険医療機関|医療機関名|病院名|医院名|薬局名|調剤薬局名)\s*[:：]?\s*"
)


class FacilityExtractor:
    def extract(self, document_type: DocumentType, lines: list[OCRLine]) -> dict[str, list[Candidate]]:
        candidates = {
            FieldName.PAYER_FACILITY_NAME: [],
            FieldName.PRESCRIBING_FACILITY_NAME: [],
        }
        if not lines:
            return candidates

        contact_lines = [line for line in lines if contains_any(line.text, CONTACT_ANCHORS)]
        prescribing_lines = [line for line in lines if contains_any(line.text, PRESCRIBING_CONTEXT)]

        for line in lines:
            cleaned = self._clean_name(line.text)
            if not self._looks_like_name(cleaned):
                continue

            if document_type == DocumentType.PHARMACY:
                payer = self._score_pharmacy_payer(line, cleaned, contact_lines, prescribing_lines)
                if payer is not None:
                    candidates[FieldName.PAYER_FACILITY_NAME].append(payer)

                prescribing = self._score_pharmacy_prescribing(line, cleaned, prescribing_lines)
                if prescribing is not None:
                    candidates[FieldName.PRESCRIBING_FACILITY_NAME].append(prescribing)
            elif document_type == DocumentType.CLINIC_OR_HOSPITAL:
                payer = self._score_clinic_payer(line, cleaned, contact_lines)
                if payer is not None:
                    candidates[FieldName.PAYER_FACILITY_NAME].append(payer)
            else:
                payer = self._score_unknown_payer(line, cleaned)
                if payer is not None:
                    candidates[FieldName.PAYER_FACILITY_NAME].append(payer)

        for key in candidates:
            candidates[key].sort(key=lambda c: c.score, reverse=True)
        return candidates

    def _score_pharmacy_payer(
        self,
        line: OCRLine,
        cleaned_text: str,
        contact_lines: list[OCRLine],
        prescribing_lines: list[OCRLine],
    ) -> Candidate | None:
        score = 1.0
        reasons: list[str] = []
        text = line.text

        if contains_any(text, PHARMACY_KEYWORDS):
            score += 3.0
            reasons.append("contains_keyword:pharmacy")
        if is_top_region(line.bbox, ratio=0.25):
            score += 2.0
            reasons.append("top_region_bonus")
        if self._near_any(line, contact_lines):
            score += 2.0
            reasons.append("near_anchor:contact")
        if contains_any(text, PRESCRIBING_CONTEXT) or self._near_any(line, prescribing_lines):
            score -= 4.0
            reasons.append("near_prescribing_context_penalty")
        if contains_any(text, CLINIC_KEYWORDS):
            score -= 2.0
            reasons.append("contains_clinic_keyword_penalty")
        if score < 1.0:
            return None

        return Candidate(
            field=FieldName.PAYER_FACILITY_NAME,
            value_raw=line.text,
            value_normalized=cleaned_text,
            source_line_indices=[line.line_index],
            bbox=line.bbox,
            score=score,
            ocr_confidence=line.confidence,
            reasons=reasons if reasons else ["facility_candidate"],
        )

    def _score_pharmacy_prescribing(
        self,
        line: OCRLine,
        cleaned_text: str,
        prescribing_lines: list[OCRLine],
    ) -> Candidate | None:
        score = 0.8
        reasons: list[str] = []
        text = line.text

        if contains_any(text, PRESCRIBING_CONTEXT) or self._near_any(line, prescribing_lines):
            score += 3.0
            reasons.append("near_prescribing_anchor")
        if contains_any(text, CLINIC_KEYWORDS):
            score += 2.0
            reasons.append("contains_clinic_keyword")
        if contains_any(text, PHARMACY_KEYWORDS):
            score -= 3.0
            reasons.append("contains_pharmacy_keyword_penalty")
        if 0.18 <= line.center()[1] <= 0.6:
            score += 0.6
            reasons.append("middle_region_bonus")
        if score < 1.0:
            return None

        return Candidate(
            field=FieldName.PRESCRIBING_FACILITY_NAME,
            value_raw=line.text,
            value_normalized=cleaned_text,
            source_line_indices=[line.line_index],
            bbox=line.bbox,
            score=score,
            ocr_confidence=line.confidence,
            reasons=reasons if reasons else ["facility_candidate"],
        )

    def _score_clinic_payer(
        self,
        line: OCRLine,
        cleaned_text: str,
        contact_lines: list[OCRLine],
    ) -> Candidate | None:
        score = 1.0
        reasons: list[str] = []
        text = line.text

        if is_top_region(line.bbox, ratio=0.25):
            score += 1.6
            reasons.append("top_region_bonus")
        if contains_any(text, CLINIC_KEYWORDS):
            score += 3.6
            reasons.append("contains_clinic_keyword")
        if "医療法人" in text:
            score += 0.8
            reasons.append("contains_medical_corporation_keyword")
        if self._near_any(line, contact_lines):
            score += 0.8
            reasons.append("near_contact_anchor")
        if contains_any(text, PRESCRIBING_CONTEXT):
            score -= 2.0
            reasons.append("prescribing_context_penalty")
        if cleaned_text.endswith(("様", "殿")):
            score -= 3.0
            reasons.append("patient_honorific_penalty")
        if score < 1.0:
            return None

        return Candidate(
            field=FieldName.PAYER_FACILITY_NAME,
            value_raw=line.text,
            value_normalized=cleaned_text,
            source_line_indices=[line.line_index],
            bbox=line.bbox,
            score=score,
            ocr_confidence=line.confidence,
            reasons=reasons if reasons else ["facility_candidate"],
        )

    def _score_unknown_payer(self, line: OCRLine, cleaned_text: str) -> Candidate | None:
        score = 0.5
        reasons: list[str] = []
        text = line.text
        if contains_any(text, PHARMACY_KEYWORDS):
            score += 1.8
            reasons.append("contains_pharmacy_keyword")
        if contains_any(text, CLINIC_KEYWORDS):
            score += 1.8
            reasons.append("contains_clinic_keyword")
        if is_top_region(line.bbox, ratio=0.3):
            score += 1.0
            reasons.append("top_region_bonus")
        if score < 1.4:
            return None

        return Candidate(
            field=FieldName.PAYER_FACILITY_NAME,
            value_raw=line.text,
            value_normalized=cleaned_text,
            source_line_indices=[line.line_index],
            bbox=line.bbox,
            score=score,
            ocr_confidence=line.confidence,
            reasons=reasons if reasons else ["facility_candidate"],
        )

    @staticmethod
    def _near_any(line: OCRLine, anchors: list[OCRLine]) -> bool:
        for anchor in anchors:
            if anchor.line_index == line.line_index:
                continue
            if is_near_line(line, anchor, vertical_tol=0.12):
                return True
        return False

    @staticmethod
    def _looks_like_name(text: str) -> bool:
        t = normalize_spaces(text)
        compact = re.sub(r"\s+", "", t)
        if not t:
            return False
        upper_t = t.upper()
        if t.startswith("〒") or upper_t.startswith("TEL") or "FAX" in upper_t:
            return False
        if len(t) < 2 or len(t) > 64:
            return False
        if any(key in t for key in NON_NAME_HINTS):
            return False
        compact_hints = tuple(key.replace(" ", "") for key in NON_NAME_HINTS)
        if any(key in compact for key in compact_hints):
            return False
        if compact in NON_NAME_EXACT:
            return False
        if compact.endswith(("様", "殿")):
            return False
        if ":" in t and not contains_any(t, PHARMACY_KEYWORDS + CLINIC_KEYWORDS):
            return False
        digits = count_digits(t)
        if digits and digits / max(1, len(t)) > 0.35:
            return False
        return True

    @staticmethod
    def _clean_name(text: str) -> str:
        cleaned = normalize_spaces(text)
        cleaned = RE_NAME_PREFIX.sub("", cleaned)
        cleaned = cleaned.strip(" :：")
        return cleaned

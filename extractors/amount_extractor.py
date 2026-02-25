from __future__ import annotations

import re

from core.enums import FieldName
from core.models import Candidate, OCRLine
from extractors.common import is_near_line, normalize_spaces

AMOUNT_LABEL_PRIMARY = ("領収", "請求", "お支払", "今回")
AMOUNT_LABEL_SECONDARY = ("合計", "計", "入金額", "金額")
AMOUNT_EXCLUDE_CONTEXT = ("総点数", "保険点数", "点数", "保険合計点", "消費税", "税率", "%")
DATE_CONTEXT = ("領収日", "発行日", "調剤日", "受診日", "診療日")
CONTACT_CONTEXT = ("TEL", "FAX", "電話", "〒")
CURRENCY_MARKERS = ("円", "¥", "￥")
IDENTIFIER_KEYWORDS = ("番号", "伝票", "受付", "会計", "患者", "カルテ")
PRIMARY_NEAR_BASE = ("領収", "請求", "合計", "自己負担")
PRIMARY_NEAR_SUFFIX = ("額", "金額")
NEGATIVE_SIGNS = ("-", "−", "－", "△", "▲", "▵")
LARGE_TEXT_HEIGHT_THRESHOLD = 0.022
SMALL_TEXT_HEIGHT_THRESHOLD = 0.012

RE_AMOUNT = re.compile(r"(?:[¥￥]\s*)?(?P<value>\d{1,3}(?:,\d{3})+|\d+)\s*(?:円)?")
RE_IDENTIFIER_NO = re.compile(r"\b(?:NO|No)\.?\s*\d", re.IGNORECASE)


class AmountExtractor:
    def extract(self, lines: list[OCRLine]) -> list[Candidate]:
        candidates: list[Candidate] = []

        for line in lines:
            text = normalize_spaces(line.text)
            matches = list(RE_AMOUNT.finditer(text))
            if not matches:
                continue

            has_primary_label = any(keyword in text for keyword in AMOUNT_LABEL_PRIMARY)
            has_secondary_label = any(keyword in text for keyword in AMOUNT_LABEL_SECONDARY)
            has_exclude_context = any(keyword in text for keyword in AMOUNT_EXCLUDE_CONTEXT)
            has_date_context = any(keyword in text for keyword in DATE_CONTEXT)
            has_contact_context = any(keyword in text.upper() for keyword in CONTACT_CONTEXT)
            near_primary_label = self._has_nearby_primary_amount_label(line, lines)
            near_secondary_label = self._has_nearby_keyword(line, lines, AMOUNT_LABEL_SECONDARY)
            near_exclude_context = self._has_nearby_keyword(line, lines, AMOUNT_EXCLUDE_CONTEXT)
            has_identifier_context = self._has_identifier_context(text)

            for match in matches:
                amount_text = match.group("value")
                value = self._parse_amount(amount_text)
                if value is None:
                    continue
                if self._is_negative_amount_match(text, match):
                    continue

                score = 1.2
                reasons: list[str] = []
                has_currency = any(marker in match.group(0) for marker in CURRENCY_MARKERS)
                if (
                    has_identifier_context
                    and not has_currency
                    and not has_primary_label
                    and not has_secondary_label
                ):
                    continue

                if has_primary_label:
                    score += 4.0
                    reasons.append("has_primary_amount_label")
                elif has_secondary_label:
                    score += 2.4
                    reasons.append("has_secondary_amount_label")

                if near_primary_label:
                    score += 2.8
                    reasons.append("near_primary_amount_label")
                elif near_secondary_label:
                    score += 1.4
                    reasons.append("near_secondary_amount_label")

                if has_currency:
                    score += 1.8
                    reasons.append("has_currency_marker")

                if has_exclude_context or near_exclude_context:
                    score -= 3.0
                    reasons.append("excluded_points_tax_context")

                if has_date_context:
                    score -= 2.0
                    reasons.append("date_context_penalty")

                if has_contact_context:
                    score -= 4.5
                    reasons.append("contact_context_penalty")

                if (
                    not has_currency
                    and not has_primary_label
                    and not has_secondary_label
                    and not near_primary_label
                    and not near_secondary_label
                ):
                    score -= 1.6
                    reasons.append("no_currency_or_amount_label_penalty")

                _, y1, _, y2 = line.bbox
                cy = (y1 + y2) / 2
                line_height = max(0.0, y2 - y1)
                if cy >= 0.55:
                    score += 0.6
                    reasons.append("bottom_region_bonus")
                if line_height >= LARGE_TEXT_HEIGHT_THRESHOLD:
                    score += 0.2
                    reasons.append("large_text_bonus")
                elif line_height <= SMALL_TEXT_HEIGHT_THRESHOLD:
                    score -= 0.2
                    reasons.append("small_text_penalty")

                if value == 0:
                    score -= 1.0
                    reasons.append("zero_amount_penalty")
                if value > 10_000_000:
                    score -= 2.0
                    reasons.append("outlier_penalty")
                if value < 10 and not any(keyword in text for keyword in AMOUNT_LABEL_PRIMARY):
                    score -= 1.0
                    reasons.append("small_amount_penalty")
                if 1900 <= value <= 2100 and not has_currency:
                    score -= 2.5
                    reasons.append("likely_year_penalty")
                if value < 100 and not has_currency and not has_primary_label:
                    score -= 1.2
                    reasons.append("small_plain_number_penalty")
                if amount_text.startswith("0") and len(amount_text) >= 3 and not has_currency:
                    score -= 1.2
                    reasons.append("leading_zero_plain_number_penalty")

                candidates.append(
                    Candidate(
                        field=FieldName.PAYMENT_AMOUNT,
                        value_raw=match.group(0),
                        value_normalized=value,
                        source_line_indices=[line.line_index],
                        bbox=line.bbox,
                        score=score,
                        ocr_confidence=line.confidence,
                        reasons=reasons if reasons else ["amount_pattern_match"],
                    )
                )

        return sorted(candidates, key=lambda c: (c.score, c.ocr_confidence), reverse=True)

    @staticmethod
    def _has_nearby_keyword(line: OCRLine, lines: list[OCRLine], keywords: tuple[str, ...]) -> bool:
        for other in lines:
            if other.line_index == line.line_index:
                continue
            text = normalize_spaces(other.text)
            if not any(keyword in text for keyword in keywords):
                continue
            if is_near_line(line, other, vertical_tol=0.06, horizontal_tol=0.8):
                return True
        return False

    @staticmethod
    def _has_nearby_primary_amount_label(line: OCRLine, lines: list[OCRLine]) -> bool:
        for other in lines:
            if other.line_index == line.line_index:
                continue
            text = normalize_spaces(other.text)
            has_base = any(keyword in text for keyword in PRIMARY_NEAR_BASE)
            has_suffix = any(keyword in text for keyword in PRIMARY_NEAR_SUFFIX)
            if not (has_base and has_suffix):
                continue
            if is_near_line(line, other, vertical_tol=0.06, horizontal_tol=0.8):
                return True
        return False

    @staticmethod
    def _parse_amount(amount_text: str) -> int | None:
        normalized = amount_text.replace(",", "").replace("，", "").strip()
        if not normalized.isdigit():
            return None
        try:
            return int(normalized)
        except ValueError:
            return None

    @staticmethod
    def _has_identifier_context(text: str) -> bool:
        if RE_IDENTIFIER_NO.search(text):
            return True
        return any(keyword in text for keyword in IDENTIFIER_KEYWORDS)

    @staticmethod
    def _is_negative_amount_match(text: str, match: re.Match[str]) -> bool:
        start = match.start()
        end = match.end()
        prefix = text[max(0, start - 4) : start].replace(" ", "")
        suffix = text[end : min(len(text), end + 2)].replace(" ", "")
        if any(prefix.endswith(sign) for sign in NEGATIVE_SIGNS):
            return True
        if prefix.endswith("(") and suffix.startswith(")"):
            return True
        return False

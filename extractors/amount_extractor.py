from __future__ import annotations

import re

from core.enums import FieldName
from core.models import Candidate, OCRLine
from extractors.common import normalize_spaces

AMOUNT_LABEL_PRIMARY = ("領収", "請求", "お支払", "今回")
AMOUNT_LABEL_SECONDARY = ("合計", "計")
AMOUNT_EXCLUDE_CONTEXT = ("総点数", "保険点数", "点数", "消費税", "税率", "%")
DATE_CONTEXT = ("領収日", "発行日", "調剤日")

RE_AMOUNT = re.compile(r"(?:[¥￥]\s*)?(?P<value>\d{1,3}(?:,\d{3})+|\d+)\s*(?:円)?")


class AmountExtractor:
    def extract(self, lines: list[OCRLine]) -> list[Candidate]:
        candidates: list[Candidate] = []

        for line in lines:
            text = normalize_spaces(line.text)
            matches = list(RE_AMOUNT.finditer(text))
            if not matches:
                continue

            for match in matches:
                amount_text = match.group("value")
                value = self._parse_amount(amount_text)
                if value is None:
                    continue

                score = 1.5
                reasons: list[str] = []

                if any(keyword in text for keyword in AMOUNT_LABEL_PRIMARY):
                    score += 4.0
                    reasons.append("has_primary_amount_label")
                elif any(keyword in text for keyword in AMOUNT_LABEL_SECONDARY):
                    score += 2.0
                    reasons.append("has_secondary_amount_label")

                if any(keyword in text for keyword in AMOUNT_EXCLUDE_CONTEXT):
                    score -= 3.0
                    reasons.append("excluded_points_tax_context")

                if any(keyword in text for keyword in DATE_CONTEXT):
                    score -= 2.0
                    reasons.append("date_context_penalty")

                _, y1, _, y2 = line.bbox
                cy = (y1 + y2) / 2
                if cy >= 0.6:
                    score += 1.0
                    reasons.append("bottom_region_bonus")

                if value == 0:
                    score -= 1.0
                    reasons.append("zero_amount_penalty")
                if value > 10_000_000:
                    score -= 2.0
                    reasons.append("outlier_penalty")
                if value < 10 and not any(keyword in text for keyword in AMOUNT_LABEL_PRIMARY):
                    score -= 1.0
                    reasons.append("small_amount_penalty")

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

        return sorted(candidates, key=lambda c: c.score, reverse=True)

    @staticmethod
    def _parse_amount(amount_text: str) -> int | None:
        normalized = amount_text.replace(",", "").replace("，", "").strip()
        if not normalized.isdigit():
            return None
        try:
            return int(normalized)
        except ValueError:
            return None


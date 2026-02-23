from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from core.enums import FieldName
from core.models import Candidate, OCRLine
from extractors.common import is_top_region, normalize_spaces

DATE_LABEL_PRIORITY = ("領収日", "発行日", "調剤日", "お会計日")
DATE_LABEL_DEPRIORITY = ("処方箋交付日", "受診日")

RE_GREGORIAN = re.compile(
    r"(?P<year>\d{4})\s*[\/\-.年]\s*(?P<month>\d{1,2})\s*[\/\-.月]\s*(?P<day>\d{1,2})\s*日?"
)
RE_REIWA_SHORT = re.compile(
    r"(?P<era>[RrHh])\s*(?P<year>\d{1,2})\s*[\/\-.年]\s*(?P<month>\d{1,2})\s*[\/\-.月]\s*(?P<day>\d{1,2})"
)
RE_REIWA_TEXT = re.compile(
    r"令和(?P<year>元|\d{1,2})年\s*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日?"
)
RE_HEISEI_TEXT = re.compile(
    r"平成(?P<year>元|\d{1,2})年\s*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日?"
)
RE_MONTH_DAY = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*[\/\-.月]\s*(?P<day>\d{1,2})\s*日?")


class DateExtractor:
    def extract(self, lines: list[OCRLine]) -> list[Candidate]:
        candidates: list[Candidate] = []
        for line in lines:
            text = normalize_spaces(line.text)
            parsed = self._parse_date(text)
            if parsed is None:
                continue

            value_normalized, parsed_date, year_missing = parsed
            score = 2.0
            reasons: list[str] = []

            if any(label in text for label in DATE_LABEL_PRIORITY):
                score += 3.0
                reasons.append("has_preferred_date_label")
            if any(label in text for label in DATE_LABEL_DEPRIORITY):
                score -= 0.7
                reasons.append("has_lower_priority_date_label")
            if is_top_region(line.bbox, ratio=0.6):
                score += 0.8
                reasons.append("top_middle_region_bonus")
            if year_missing:
                score -= 2.0
                reasons.append("year_missing_hold_candidate")
            elif parsed_date is not None and parsed_date > date.today() + timedelta(days=7):
                score -= 2.0
                reasons.append("future_date_penalty")

            candidates.append(
                Candidate(
                    field=FieldName.PAYMENT_DATE,
                    value_raw=text,
                    value_normalized=value_normalized,
                    source_line_indices=[line.line_index],
                    bbox=line.bbox,
                    score=score,
                    ocr_confidence=line.confidence,
                    reasons=reasons if reasons else ["date_pattern_match"],
                )
            )
        return sorted(candidates, key=lambda c: c.score, reverse=True)

    def _parse_date(self, text: str) -> Optional[tuple[str, Optional[date], bool]]:
        for regex in (RE_GREGORIAN,):
            match = regex.search(text)
            if match:
                parsed = self._build_date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
                if parsed is not None:
                    return (parsed.isoformat(), parsed, False)

        match = RE_REIWA_SHORT.search(text)
        if match:
            year = int(match.group("year"))
            month = int(match.group("month"))
            day = int(match.group("day"))
            if match.group("era").lower() == "r":
                parsed = self._build_date(2018 + year, month, day)
            else:
                parsed = self._build_date(1988 + year, month, day)
            if parsed is not None:
                return (parsed.isoformat(), parsed, False)

        match = RE_REIWA_TEXT.search(text)
        if match:
            year_text = match.group("year")
            year = 1 if year_text == "元" else int(year_text)
            parsed = self._build_date(2018 + year, int(match.group("month")), int(match.group("day")))
            if parsed is not None:
                return (parsed.isoformat(), parsed, False)

        match = RE_HEISEI_TEXT.search(text)
        if match:
            year_text = match.group("year")
            year = 1 if year_text == "元" else int(year_text)
            parsed = self._build_date(1988 + year, int(match.group("month")), int(match.group("day")))
            if parsed is not None:
                return (parsed.isoformat(), parsed, False)

        match = RE_MONTH_DAY.search(text)
        if match:
            month = int(match.group("month"))
            day = int(match.group("day"))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return (f"{month:02d}-{day:02d}", None, True)

        return None

    @staticmethod
    def _build_date(year: int, month: int, day: int) -> Optional[date]:
        try:
            return datetime(year=year, month=month, day=day).date()
        except ValueError:
            return None


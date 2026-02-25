from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from core.enums import FieldName
from core.models import Candidate, OCRLine
from extractors.common import is_near_line, is_top_region, merge_bboxes, normalize_spaces

DATE_LABEL_PRIORITY = ("領収日", "発行日", "調剤日", "お会計日")
DATE_LABEL_DEPRIORITY = ("処方箋交付日", "受診日")

RE_GREGORIAN = re.compile(
    r"(?P<year>\d{4})\s*[\/\-.年]\s*(?P<month>\d{1,2})\s*[\/\-.月]\s*(?P<day>\d{1,2})\s*日?"
)
RE_ERALESS_SHORT_YMD = re.compile(
    r"(?<!\d)(?P<year>\d{1,2})\s*[\/\-.]\s*(?P<month>\d{1,2})\s*[\/\-.]\s*(?P<day>\d{1,2})(?!\d)"
)
RE_REIWA_SHORT = re.compile(
    r"(?P<era>[RrHh])\s*(?P<year>\d{1,2})\s*[\/\-.年]\s*(?P<month>\d{1,2})\s*[\/\-.月]\s*(?P<day>\d{1,2})"
)
RE_REIWA_TEXT = re.compile(
    r"令和\s*(?P<year>元|\d{1,2})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日?"
)
RE_HEISEI_TEXT = re.compile(
    r"平成\s*(?P<year>元|\d{1,2})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日?"
)
RE_MONTH_DAY = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*[\/\-.月]\s*(?P<day>\d{1,2})\s*日?")


class DateExtractor:
    def extract(self, lines: list[OCRLine]) -> list[Candidate]:
        candidates: list[Candidate] = []
        preferred_label_lines = [
            line for line in lines if any(label in normalize_spaces(line.text) for label in DATE_LABEL_PRIORITY)
        ]
        lower_priority_label_lines = [
            line for line in lines if any(label in normalize_spaces(line.text) for label in DATE_LABEL_DEPRIORITY)
        ]

        for line in lines:
            text = normalize_spaces(line.text)
            parsed = self._parse_date(text)
            if parsed is None:
                continue

            value_normalized, parsed_date, year_missing = parsed
            score = 2.0
            reasons: list[str] = []
            source_line_indices = [line.line_index]
            candidate_bbox = line.bbox

            if any(label in text for label in DATE_LABEL_PRIORITY):
                score += 3.0
                reasons.append("has_preferred_date_label")
            else:
                nearby_label = self._find_nearby_label_line(line, preferred_label_lines)
                if nearby_label is not None:
                    score += 2.2
                    reasons.append("near_preferred_date_label")
                    source_line_indices.append(nearby_label.line_index)
                    merged = merge_bboxes([candidate_bbox, nearby_label.bbox])
                    if merged is not None:
                        candidate_bbox = merged

            if any(label in text for label in DATE_LABEL_DEPRIORITY):
                score -= 0.7
                reasons.append("has_lower_priority_date_label")
            else:
                nearby_lower = self._find_nearby_label_line(line, lower_priority_label_lines)
                if nearby_lower is not None:
                    score -= 0.5
                    reasons.append("near_lower_priority_date_label")
                    source_line_indices.append(nearby_lower.line_index)
                    merged = merge_bboxes([candidate_bbox, nearby_lower.bbox])
                    if merged is not None:
                        candidate_bbox = merged

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
                    source_line_indices=sorted(set(source_line_indices)),
                    bbox=candidate_bbox,
                    score=score,
                    ocr_confidence=line.confidence,
                    reasons=reasons if reasons else ["date_pattern_match"],
                )
            )
        return sorted(candidates, key=lambda c: c.score, reverse=True)

    @staticmethod
    def _find_nearby_label_line(target: OCRLine, labels: list[OCRLine]) -> OCRLine | None:
        for label in labels:
            if label.line_index == target.line_index:
                continue
            if is_near_line(target, label, vertical_tol=0.04, horizontal_tol=0.7):
                return label
        return None

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

        match = RE_ERALESS_SHORT_YMD.search(text)
        if match:
            parsed = self._resolve_era_without_marker(
                year=int(match.group("year")),
                month=int(match.group("month")),
                day=int(match.group("day")),
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

    @staticmethod
    def _resolve_era_without_marker(year: int, month: int, day: int) -> Optional[date]:
        if year <= 0:
            return None

        candidates: list[date] = []
        reiwa = DateExtractor._build_date(2018 + year, month, day)
        heisei = DateExtractor._build_date(1988 + year, month, day)
        if reiwa is not None:
            candidates.append(reiwa)
        if heisei is not None:
            candidates.append(heisei)
        if not candidates:
            return None

        today = date.today()
        horizon = today + timedelta(days=31)
        non_future = [d for d in candidates if d <= horizon]
        pool = non_future if non_future else candidates
        return min(pool, key=lambda d: abs((today - d).days))

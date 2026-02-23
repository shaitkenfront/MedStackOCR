from __future__ import annotations

import re
from typing import Any

from core.models import Candidate, OCRLine, TemplateMatch
from extractors.date_extractor import DateExtractor
from extractors.common import is_near_line
from templates.fingerprint import bbox_distance, line_in_bbox
from templates.store import TemplateStore


class TemplateMatcher:
    def __init__(self, store: TemplateStore, match_threshold: float = 0.65) -> None:
        self.store = store
        self.match_threshold = match_threshold
        self._date_extractor = DateExtractor()

    def match(
        self,
        household_id: str,
        document_type: str,
        lines: list[OCRLine],
    ) -> tuple[TemplateMatch, dict[str, Any] | None]:
        templates = self.store.load_household_templates(household_id, document_type=document_type)
        if not templates:
            return TemplateMatch(matched=False, template_family_id=None, score=0.0, reasons=["no_templates"]), None

        best_template: dict[str, Any] | None = None
        best_score = 0.0
        best_reasons: list[str] = []

        for template in templates:
            score, reasons = self._score_template(template, lines)
            if score > best_score:
                best_score = score
                best_template = template
                best_reasons = reasons

        if best_template and best_score >= self.match_threshold:
            return (
                TemplateMatch(
                    matched=True,
                    template_family_id=str(best_template.get("template_family_id")),
                    score=best_score,
                    reasons=best_reasons,
                ),
                best_template,
            )
        return (
            TemplateMatch(
                matched=False,
                template_family_id=None,
                score=best_score,
                reasons=best_reasons or ["template_score_below_threshold"],
            ),
            None,
        )

    def _score_template(self, template: dict[str, Any], lines: list[OCRLine]) -> tuple[float, list[str]]:
        anchors = template.get("anchors", [])
        if not anchors:
            return 0.0, ["template_has_no_anchors"]

        matched_anchors = 0
        position_score_sum = 0.0
        reasons: list[str] = []

        for anchor in anchors:
            pattern = str(anchor.get("text_pattern", "")).strip()
            if not pattern:
                continue
            matched_lines = [line for line in lines if pattern in line.text]
            if not matched_lines:
                reasons.append(f"anchor_miss:{pattern}")
                continue
            matched_anchors += 1
            reasons.append(f"anchor_hit:{pattern}")
            bbox = anchor.get("bbox")
            if isinstance(bbox, list) and len(bbox) == 4:
                expected = tuple(float(v) for v in bbox)
                distance = min(bbox_distance(line.bbox, expected) for line in matched_lines)
                position_score_sum += max(0.0, 1.0 - distance / 0.5)
            else:
                position_score_sum += 0.5

        anchor_count = max(1, len(anchors))
        text_ratio = matched_anchors / anchor_count
        position_ratio = position_score_sum / anchor_count
        score = 0.7 * text_ratio + 0.3 * position_ratio
        reasons.append(f"template_score:{score:.3f}")
        return score, reasons

    def apply_template(
        self,
        template: dict[str, Any],
        lines: list[OCRLine],
    ) -> dict[str, list[Candidate]]:
        field_specs = template.get("field_specs", {})
        if not isinstance(field_specs, dict):
            return {}

        anchors = template.get("anchors", [])
        anchor_lines = {
            str(anchor.get("text_pattern", "")): [
                line for line in lines if str(anchor.get("text_pattern", "")) in line.text
            ]
            for anchor in anchors
            if str(anchor.get("text_pattern", "")).strip()
        }
        candidates: dict[str, list[Candidate]] = {}

        for field_name, spec_raw in field_specs.items():
            if not isinstance(spec_raw, dict):
                continue
            target_bbox_raw = spec_raw.get("target_bbox")
            if not isinstance(target_bbox_raw, list) or len(target_bbox_raw) != 4:
                continue
            target_bbox = tuple(float(v) for v in target_bbox_raw)
            rules = [str(rule) for rule in spec_raw.get("selection_rules", []) if isinstance(rule, str)]

            lines_in_bbox = [line for line in lines if line_in_bbox(line, target_bbox)]
            if not lines_in_bbox:
                continue

            field_candidates: list[Candidate] = []
            for line in lines_in_bbox:
                score = 2.5
                reasons = ["template_target_bbox_match"]

                for rule in rules:
                    score, reasons = self._apply_rule(
                        score=score,
                        reasons=reasons,
                        rule=rule,
                        line=line,
                        anchors=anchor_lines,
                    )

                value_normalized = self._normalize_field_value(field_name, line.text)
                if value_normalized is None:
                    continue

                field_candidates.append(
                    Candidate(
                        field=field_name,
                        value_raw=line.text,
                        value_normalized=value_normalized,
                        source_line_indices=[line.line_index],
                        bbox=line.bbox,
                        score=score,
                        ocr_confidence=line.confidence,
                        reasons=reasons,
                        source="template",
                    )
                )

            if field_candidates:
                candidates[field_name] = sorted(field_candidates, key=lambda c: c.score, reverse=True)[:3]
        return candidates

    def _apply_rule(
        self,
        score: float,
        reasons: list[str],
        rule: str,
        line: OCRLine,
        anchors: dict[str, list[OCRLine]],
    ) -> tuple[float, list[str]]:
        if rule == "topmost_text":
            score += max(0.0, 1.0 - line.center()[1])
            reasons.append("rule:topmost_text")
            return score, reasons

        if rule == "prefer_near_anchor":
            anchor_found = False
            for anchor_lines in anchors.values():
                if any(is_near_line(line, anchor, vertical_tol=0.15) for anchor in anchor_lines):
                    anchor_found = True
                    break
            if anchor_found:
                score += 1.2
                reasons.append("rule:prefer_near_anchor")
            return score, reasons

        if rule.startswith("prefer_keyword:"):
            keywords = self._split_keywords(rule)
            if any(keyword in line.text for keyword in keywords):
                score += 1.8
                reasons.append(f"rule:prefer_keyword:{','.join(keywords)}")
            return score, reasons

        if rule.startswith("prefer_label:"):
            labels = self._split_keywords(rule)
            if any(label in line.text for label in labels):
                score += 1.4
                reasons.append(f"rule:prefer_label:{','.join(labels)}")
            return score, reasons

        if rule == "parse_date":
            parsed = self._date_extractor._parse_date(line.text)  # noqa: SLF001
            if parsed is not None and not parsed[2]:
                score += 0.8
                reasons.append("rule:parse_date_ok")
            else:
                score -= 0.8
                reasons.append("rule:parse_date_failed")
            return score, reasons

        if rule == "parse_amount":
            amount = self._normalize_amount(line.text)
            if amount is not None:
                score += 0.8
                reasons.append("rule:parse_amount_ok")
            else:
                score -= 0.8
                reasons.append("rule:parse_amount_failed")
            return score, reasons

        return score, reasons

    @staticmethod
    def _split_keywords(rule: str) -> list[str]:
        _, value = rule.split(":", 1)
        return [item.strip() for item in value.split(",") if item.strip()]

    def _normalize_field_value(self, field_name: str, text: str) -> Any | None:
        if field_name == "payment_amount":
            return self._normalize_amount(text)

        if field_name == "payment_date":
            parsed = self._date_extractor._parse_date(text)  # noqa: SLF001
            if parsed is None:
                return None
            return parsed[0]

        cleaned = re.sub(r"\s+", " ", text).strip()
        return cleaned or None

    @staticmethod
    def _normalize_amount(text: str) -> int | None:
        match = re.search(r"(?:[¥￥]\s*)?(\d{1,3}(?:,\d{3})+|\d+)\s*(?:円)?", text)
        if not match:
            return None
        normalized = match.group(1).replace(",", "")
        if not normalized.isdigit():
            return None
        return int(normalized)


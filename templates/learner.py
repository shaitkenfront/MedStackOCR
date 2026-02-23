from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.models import OCRLine
from templates.fingerprint import find_nearest_line, sanitize_anchor_text
from templates.store import TemplateStore


DEFAULT_FIELD_RULES = {
    "payer_facility_name": ["topmost_text", "prefer_keyword:薬局,調剤,病院,医院,クリニック"],
    "prescribing_facility_name": ["prefer_near_anchor", "prefer_keyword:病院,医院,クリニック"],
    "payment_date": ["prefer_label:領収日,発行日,調剤日", "parse_date"],
    "payment_amount": ["prefer_label:領収,請求,お支払,合計,計", "parse_amount"],
}


class TemplateLearner:
    def __init__(self, store: TemplateStore) -> None:
        self.store = store

    def learn_from_review(
        self,
        document_result: dict[str, Any],
        review_fix: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        household_id = str(review_fix.get("household_id") or document_result.get("household_id"))
        if not household_id:
            raise ValueError("household_id is required in review_fix or document_result")

        document_type = str(document_result.get("document_type", "unknown"))
        current_family_id = (
            document_result.get("template_match", {}).get("template_family_id")
            if isinstance(document_result.get("template_match"), dict)
            else None
        )
        template_family_id = (
            str(current_family_id)
            if current_family_id
            else f"{document_type}_family_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        )

        lines = self._parse_lines(document_result.get("ocr_lines", []))
        corrections = review_fix.get("corrections", {})
        if not isinstance(corrections, dict) or not corrections:
            raise ValueError("review_fix.corrections is empty")

        existing = self.store.get_template(household_id, template_family_id) or {}
        anchors, field_specs = self._build_template_parts(corrections, lines, existing)

        sample_count = int(existing.get("sample_count", 0)) + 1
        prev_rate = float(existing.get("success_rate", 0.8))
        success_rate = ((prev_rate * (sample_count - 1)) + 1.0) / sample_count

        template = {
            "template_family_id": template_family_id,
            "scope": "household",
            "household_id": household_id,
            "document_type": document_type,
            "anchors": anchors,
            "field_specs": field_specs,
            "sample_count": sample_count,
            "success_rate": round(success_rate, 4),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        path = self.store.save_template(template)
        return template, str(path)

    def _build_template_parts(
        self,
        corrections: dict[str, Any],
        lines: list[OCRLine],
        existing: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        anchors: list[dict[str, Any]] = []
        anchor_index: dict[str, int] = {}
        existing_anchors = existing.get("anchors", [])
        if isinstance(existing_anchors, list):
            for anchor in existing_anchors:
                if not isinstance(anchor, dict):
                    continue
                text_pattern = str(anchor.get("text_pattern", "")).strip()
                bbox = anchor.get("bbox")
                if not text_pattern or not isinstance(bbox, list) or len(bbox) != 4:
                    continue
                anchor_index[text_pattern] = len(anchors)
                anchors.append({"text_pattern": text_pattern, "bbox": [float(v) for v in bbox]})

        existing_specs = existing.get("field_specs", {})
        field_specs = dict(existing_specs) if isinstance(existing_specs, dict) else {}

        for field_name, correction in corrections.items():
            if not isinstance(correction, dict):
                continue
            bbox_raw = correction.get("bbox")
            if not isinstance(bbox_raw, list) or len(bbox_raw) != 4:
                continue
            target_bbox = tuple(float(v) for v in bbox_raw)

            nearest_line = find_nearest_line(lines, target_bbox)
            anchor_text = ""
            if nearest_line is not None:
                anchor_text = sanitize_anchor_text(nearest_line.text)
            if not anchor_text:
                anchor_text = sanitize_anchor_text(str(correction.get("value", "")))

            anchor_refs: list[str] = []
            if anchor_text:
                anchor_refs.append(anchor_text)
                if anchor_text not in anchor_index:
                    anchor_bbox = list(nearest_line.bbox) if nearest_line is not None else list(target_bbox)
                    anchor_index[anchor_text] = len(anchors)
                    anchors.append({"text_pattern": anchor_text, "bbox": anchor_bbox})

            field_specs[field_name] = {
                "target_bbox": list(target_bbox),
                "anchor_refs": anchor_refs,
                "selection_rules": DEFAULT_FIELD_RULES.get(field_name, ["prefer_near_anchor"]),
            }
        return anchors, field_specs

    @staticmethod
    def _parse_lines(raw_lines: Any) -> list[OCRLine]:
        if not isinstance(raw_lines, list):
            return []
        lines: list[OCRLine] = []
        for idx, row in enumerate(raw_lines):
            if not isinstance(row, dict):
                continue
            text = str(row.get("text", "")).strip()
            bbox_raw = row.get("bbox")
            if not text or not isinstance(bbox_raw, list) or len(bbox_raw) != 4:
                continue
            bbox = tuple(float(v) for v in bbox_raw)
            confidence = float(row.get("confidence", 0.0))
            line_index = int(row.get("line_index", idx))
            page = int(row.get("page", 1))
            lines.append(
                OCRLine(
                    text=text,
                    bbox=bbox,
                    polygon=None,
                    confidence=confidence,
                    line_index=line_index,
                    page=page,
                    raw=row,
                )
            )
        return lines


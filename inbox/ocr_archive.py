"""OCR で読み取った行の保存形式。

領収書画像と同じ場所に `<画像のパス>.ocr.json` として保存する。
画像と同じ保存期間（S3 のライフサイクル）で消え、画像を削除するときは一緒に削除する。
抽出ロジックを改善するときに、OCR を呼び直さずに評価できるようにするためのもの。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.models import ExtractionResult, OCRLine

OCR_ARCHIVE_SUFFIX = ".ocr.json"
SCHEMA_VERSION = 1


def archive_path_for(image_path: str) -> str:
    path = str(image_path or "").strip()
    return f"{path}{OCR_ARCHIVE_SUFFIX}" if path else ""


def build_ocr_archive(receipt_id: str, result: ExtractionResult) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for field_name, candidate in result.fields.items():
        if candidate is not None:
            selected[field_name] = candidate.value_normalized
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "engine": result.audit.engine,
        "engine_version": result.audit.engine_version,
        "document_type": result.document_type.value,
        "decision": result.decision.status.value,
        "selected": selected,
        "lines": [
            {
                "text": line.text,
                "bbox": list(line.bbox),
                "confidence": line.confidence,
                "line_index": line.line_index,
                "page": line.page,
            }
            for line in result.ocr_lines
        ],
    }


def lines_from_archive(data: dict[str, Any]) -> list[OCRLine]:
    lines: list[OCRLine] = []
    for row in data.get("lines") or []:
        bbox = row.get("bbox") or [0, 0, 0, 0]
        lines.append(
            OCRLine(
                text=str(row.get("text") or ""),
                bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                polygon=None,
                confidence=float(row.get("confidence") or 0.0),
                line_index=int(row.get("line_index") or 0),
                page=int(row.get("page") or 1),
            )
        )
    return lines

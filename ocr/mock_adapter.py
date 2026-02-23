from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.models import OCRRawResult


class MockOCRAdapter:
    name = "mock"
    version = "1.0.0"

    def __init__(self, fixture_dir: str | None = None) -> None:
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None

    def healthcheck(self) -> bool:
        return True

    def run(self, image_path: str) -> OCRRawResult:
        image = Path(image_path)
        payload = self._load_sidecar(image)
        if payload is None:
            payload = self._default_payload(image.name.lower())

        return OCRRawResult(
            engine=self.name,
            engine_version=self.version,
            payload=payload,
            metadata={"source_image": str(image)},
        )

    def _load_sidecar(self, image: Path) -> Any | None:
        candidates: list[Path] = []
        if self.fixture_dir:
            candidates.append(self.fixture_dir / f"{image.stem}.ocr.json")
        candidates.append(image.with_suffix(image.suffix + ".ocr.json"))
        candidates.append(image.with_name(f"{image.stem}.ocr.json"))

        for candidate in candidates:
            if not candidate.exists():
                continue
            text = candidate.read_text(encoding="utf-8")
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "lines" in parsed:
                return parsed["lines"]
            return parsed
        return None

    def _default_payload(self, filename: str) -> list[dict[str, Any]]:
        if "clinic" in filename or "hospital" in filename:
            return self._default_clinic_payload()
        return self._default_pharmacy_payload()

    @staticmethod
    def _default_pharmacy_payload() -> list[dict[str, Any]]:
        return [
            {
                "text": "〇〇調剤薬局",
                "bbox": [0.06, 0.03, 0.60, 0.09],
                "confidence": 0.96,
                "line_index": 0,
                "page": 1,
            },
            {
                "text": "〒123-4567 東京都千代田区1-2-3",
                "bbox": [0.06, 0.10, 0.75, 0.15],
                "confidence": 0.91,
                "line_index": 1,
                "page": 1,
            },
            {
                "text": "TEL 03-1234-5678",
                "bbox": [0.06, 0.16, 0.45, 0.20],
                "confidence": 0.92,
                "line_index": 2,
                "page": 1,
            },
            {
                "text": "領収日 2026/02/22",
                "bbox": [0.52, 0.18, 0.94, 0.23],
                "confidence": 0.95,
                "line_index": 3,
                "page": 1,
            },
            {
                "text": "処方箋交付医療機関 △△内科クリニック",
                "bbox": [0.08, 0.34, 0.92, 0.40],
                "confidence": 0.90,
                "line_index": 4,
                "page": 1,
            },
            {
                "text": "今回お支払額 ¥1,840",
                "bbox": [0.58, 0.87, 0.96, 0.94],
                "confidence": 0.94,
                "line_index": 5,
                "page": 1,
            },
        ]

    @staticmethod
    def _default_clinic_payload() -> list[dict[str, Any]]:
        return [
            {
                "text": "△△内科クリニック",
                "bbox": [0.08, 0.04, 0.62, 0.10],
                "confidence": 0.96,
                "line_index": 0,
                "page": 1,
            },
            {
                "text": "TEL 03-9999-0000",
                "bbox": [0.08, 0.11, 0.48, 0.16],
                "confidence": 0.90,
                "line_index": 1,
                "page": 1,
            },
            {
                "text": "領収日 2026-02-21",
                "bbox": [0.54, 0.20, 0.93, 0.25],
                "confidence": 0.92,
                "line_index": 2,
                "page": 1,
            },
            {
                "text": "請求額 3,200円",
                "bbox": [0.60, 0.86, 0.96, 0.92],
                "confidence": 0.94,
                "line_index": 3,
                "page": 1,
            },
        ]


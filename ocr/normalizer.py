from __future__ import annotations

from typing import Any

from core.models import OCRLine, OCRRawResult


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


class OCRNormalizer:
    def normalize(self, raw: OCRRawResult, image_size: tuple[int, int]) -> list[OCRLine]:
        width, height = image_size
        width = max(1, int(width))
        height = max(1, int(height))

        payload = raw.payload
        if isinstance(payload, dict) and "lines" in payload:
            rows = payload["lines"]
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []

        normalized: list[OCRLine] = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            text = str(row.get("text", "")).strip()
            if not text:
                continue
            bbox = self._normalize_bbox(row.get("bbox"), width, height)
            if bbox is None:
                continue
            polygon = self._normalize_polygon(row.get("polygon"), width, height)
            confidence = self._normalize_confidence(row.get("confidence", 0.0))
            line_index = int(row.get("line_index", idx))
            page = int(row.get("page", 1))
            normalized.append(
                OCRLine(
                    text=text,
                    bbox=bbox,
                    polygon=polygon,
                    confidence=confidence,
                    line_index=line_index,
                    page=page,
                    raw=row,
                )
            )

        normalized.sort(key=lambda line: (line.page, line.line_index, line.bbox[1], line.bbox[0]))
        return normalized

    @staticmethod
    def _normalize_confidence(value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            score = 0.0
        if score > 1.0:
            score /= 100.0
        return _clamp01(score)

    @staticmethod
    def _normalize_bbox(value: Any, width: int, height: int) -> tuple[float, float, float, float] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        try:
            x1, y1, x2, y2 = (float(v) for v in value)
        except (TypeError, ValueError):
            return None

        absolute = any(v > 1.5 for v in (x1, y1, x2, y2))
        if absolute:
            x1, y1, x2, y2 = (x1 / width, y1 / height, x2 / width, y2 / height)

        x_left = _clamp01(min(x1, x2))
        x_right = _clamp01(max(x1, x2))
        y_top = _clamp01(min(y1, y2))
        y_bottom = _clamp01(max(y1, y2))
        return (x_left, y_top, x_right, y_bottom)

    @staticmethod
    def _normalize_polygon(value: Any, width: int, height: int) -> list[tuple[float, float]] | None:
        if not isinstance(value, (list, tuple)):
            return None
        points: list[tuple[float, float]] = []
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            try:
                x, y = float(item[0]), float(item[1])
            except (TypeError, ValueError):
                continue
            if x > 1.5 or y > 1.5:
                x, y = x / width, y / height
            points.append((_clamp01(x), _clamp01(y)))
        return points if points else None


from __future__ import annotations

import math
import re
from typing import Iterable

from core.models import BBox, OCRLine


def contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def count_digits(text: str) -> int:
    return sum(1 for ch in text if ch.isdigit())


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def merge_bboxes(bboxes: list[BBox]) -> BBox | None:
    if not bboxes:
        return None
    x1 = min(b[0] for b in bboxes)
    y1 = min(b[1] for b in bboxes)
    x2 = max(b[2] for b in bboxes)
    y2 = max(b[3] for b in bboxes)
    return (x1, y1, x2, y2)


def bbox_center(bbox: BBox) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def is_top_region(bbox: BBox, ratio: float = 0.25) -> bool:
    _, cy = bbox_center(bbox)
    return cy <= ratio


def vertical_distance(b1: BBox, b2: BBox) -> float:
    _, y1 = bbox_center(b1)
    _, y2 = bbox_center(b2)
    return abs(y1 - y2)


def horizontal_distance(b1: BBox, b2: BBox) -> float:
    x1, _ = bbox_center(b1)
    x2, _ = bbox_center(b2)
    return abs(x1 - x2)


def is_near_line(line: OCRLine, other: OCRLine, vertical_tol: float = 0.08, horizontal_tol: float = 0.5) -> bool:
    return (
        vertical_distance(line.bbox, other.bbox) <= vertical_tol
        and horizontal_distance(line.bbox, other.bbox) <= horizontal_tol
    )


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_to_unit(score: float, low: float = 0.0, high: float = 10.0) -> float:
    if high <= low:
        return 0.0
    return clamp01((score - low) / (high - low))


def logistic(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


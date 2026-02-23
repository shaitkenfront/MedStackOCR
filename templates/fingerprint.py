from __future__ import annotations

import math
import re
from typing import Any

from core.models import BBox, OCRLine


def bbox_center(bbox: BBox) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def bbox_distance(b1: BBox, b2: BBox) -> float:
    x1, y1 = bbox_center(b1)
    x2, y2 = bbox_center(b2)
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


def sanitize_anchor_text(text: str, max_length: int = 12) -> str:
    compact = re.sub(r"\s+", "", text).strip(" :：-")
    compact = re.sub(r"[0-9０-９,，./／:：¥￥\-ー]+", "", compact)
    compact = compact.strip(" :：-")
    if len(compact) >= 2:
        return compact[:max_length]
    fallback = re.sub(r"\s+", "", text).strip(" :：-")
    return fallback[:max_length]


def find_nearest_line(lines: list[OCRLine], target_bbox: BBox, max_distance: float = 0.2) -> OCRLine | None:
    nearest: OCRLine | None = None
    nearest_distance = float("inf")
    for line in lines:
        dist = bbox_distance(line.bbox, target_bbox)
        if dist < nearest_distance:
            nearest = line
            nearest_distance = dist
    if nearest is None or nearest_distance > max_distance:
        return None
    return nearest


def point_in_bbox(point: tuple[float, float], bbox: BBox) -> bool:
    x, y = point
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def line_in_bbox(line: OCRLine, bbox: BBox) -> bool:
    return point_in_bbox(line.center(), bbox)

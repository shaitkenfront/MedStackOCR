from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Any

TEXT_KEYS = (
    "text",
    "transcription",
    "content",
    "line",
    "ocr_text",
    "value",
)
CONFIDENCE_KEYS = ("confidence", "score", "probability", "prob", "rec_score")
BBOX_KEYS = ("bbox", "boundingBox", "bounding_box", "box")
POLYGON_KEYS = ("polygon", "points", "img_coordinates", "vertices")


def split_command(command: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(command, str):
        text = command.strip()
        if not text:
            return []
        return shlex.split(text, posix=os.name != "nt")
    if isinstance(command, (list, tuple)):
        return [str(item) for item in command if str(item).strip()]
    return []


def command_exists(command_parts: list[str], working_dir: str | None = None) -> bool:
    if not command_parts:
        return False

    executable = command_parts[0]
    if _has_path_separator(executable) or Path(executable).suffix:
        if not _resolve_path(executable, working_dir).exists():
            return False
    elif shutil.which(executable) is None:
        return False

    if len(command_parts) > 1 and command_parts[1].endswith(".py"):
        if not _resolve_path(command_parts[1], working_dir).exists():
            return False

    return True


def find_output_candidates(
    base_dir: Path,
    stem: str,
    suffixes: tuple[str, ...],
    recursive: bool = True,
) -> list[Path]:
    walker = base_dir.rglob if recursive else base_dir.glob

    exact: list[Path] = []
    for suffix in suffixes:
        exact.extend(walker(f"{stem}{suffix}"))
    if exact:
        return _sort_paths(exact)

    wildcard: list[Path] = []
    for suffix in suffixes:
        wildcard.extend(walker(f"*{suffix}"))
    return _sort_paths(wildcard)


def parse_ocr_payload(payload: Any) -> list[dict[str, Any]]:
    rows = _collect_rows(payload)
    extracted: list[dict[str, Any]] = []
    for row in rows:
        text = _extract_text(row)
        if not text:
            continue
        polygon = _extract_polygon(row)
        bbox = _extract_bbox(row, polygon)
        extracted.append(
            {
                "text": text,
                "bbox": bbox,
                "polygon": polygon,
                "confidence": _extract_confidence(row),
                "page": _as_int(row.get("page"), default=1),
                "raw": row,
            }
        )

    total = max(1, len(extracted))
    lines: list[dict[str, Any]] = []
    for idx, item in enumerate(extracted):
        bbox = item["bbox"] if item["bbox"] is not None else _synthetic_bbox(idx, total)
        line: dict[str, Any] = {
            "text": item["text"],
            "bbox": bbox,
            "confidence": item["confidence"],
            "line_index": idx,
            "page": item["page"],
            "raw": item["raw"],
        }
        if item["polygon"] is not None:
            line["polygon"] = item["polygon"]
        lines.append(line)
    return lines


def parse_text_payload(text: str) -> list[dict[str, Any]]:
    texts = [line.strip() for line in text.splitlines() if line.strip()]
    total = max(1, len(texts))
    lines: list[dict[str, Any]] = []
    for idx, content in enumerate(texts):
        lines.append(
            {
                "text": content,
                "bbox": _synthetic_bbox(idx, total),
                "confidence": 0.0,
                "line_index": idx,
                "page": 1,
            }
        )
    return lines


def _collect_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return _flatten_dicts(payload)

    if not isinstance(payload, dict):
        return []

    for key in ("lines", "contents", "items", "results", "predictions", "data"):
        value = payload.get(key)
        rows = _flatten_dicts(value)
        if rows:
            return rows

    if _looks_like_line(payload):
        return [payload]

    return []


def _flatten_dicts(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if _looks_like_line(value):
            rows.append(value)
        return rows
    if isinstance(value, (list, tuple)):
        for item in value:
            rows.extend(_flatten_dicts(item))
    return rows


def _looks_like_line(value: dict[str, Any]) -> bool:
    return any(key in value for key in TEXT_KEYS)


def _extract_text(row: dict[str, Any]) -> str:
    for key in TEXT_KEYS:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _extract_confidence(row: dict[str, Any]) -> float:
    for key in CONFIDENCE_KEYS:
        value = row.get(key)
        if value is None:
            continue
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if score > 1.0:
            score /= 100.0
        return _clamp01(score)
    return 0.0


def _extract_bbox(row: dict[str, Any], polygon: list[list[float]] | None) -> list[float] | None:
    for key in BBOX_KEYS:
        bbox = _to_bbox(row.get(key))
        if bbox is not None:
            return bbox

    if polygon is not None:
        return _polygon_to_bbox(polygon)

    for key in POLYGON_KEYS:
        candidate = _to_polygon(row.get(key))
        if candidate is not None:
            return _polygon_to_bbox(candidate)

    return None


def _extract_polygon(row: dict[str, Any]) -> list[list[float]] | None:
    for key in POLYGON_KEYS:
        polygon = _to_polygon(row.get(key))
        if polygon is not None:
            return polygon

    for key in BBOX_KEYS:
        bbox_value = row.get(key)
        if isinstance(bbox_value, (list, tuple)) and len(bbox_value) == 4:
            if all(isinstance(item, (list, tuple)) and len(item) == 2 for item in bbox_value):
                polygon = _to_polygon(bbox_value)
                if polygon is not None:
                    return polygon
    return None


def _to_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None

    if all(isinstance(item, (list, tuple)) and len(item) == 2 for item in value):
        polygon = _to_polygon(value)
        if polygon is None:
            return None
        return _polygon_to_bbox(polygon)

    numbers: list[float] = []
    for item in value:
        try:
            numbers.append(float(item))
        except (TypeError, ValueError):
            return None
    return numbers


def _to_polygon(value: Any) -> list[list[float]] | None:
    if not isinstance(value, (list, tuple)):
        return None

    points: list[list[float]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            x = float(item[0])
            y = float(item[1])
        except (TypeError, ValueError):
            continue
        points.append([x, y])

    if len(points) < 3:
        return None
    return points


def _polygon_to_bbox(polygon: list[list[float]]) -> list[float]:
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def _synthetic_bbox(index: int, total: int) -> list[float]:
    count = max(1, total)
    y1 = index / count
    y2 = (index + 1) / count
    if y2 <= y1:
        y2 = min(1.0, y1 + 1e-6)
    return [0.0, _clamp01(y1), 1.0, _clamp01(y2)]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _as_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_path(path_text: str, working_dir: str | None) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    if working_dir:
        return Path(working_dir) / path
    return path


def _has_path_separator(path_text: str) -> bool:
    return "/" in path_text or "\\" in path_text


def _sort_paths(paths: list[Path]) -> list[Path]:
    deduped = list(dict.fromkeys(paths))
    return sorted(deduped, key=_path_sort_key, reverse=True)


def _path_sort_key(path: Path) -> tuple[float, str]:
    try:
        return (path.stat().st_mtime, str(path))
    except OSError:
        return (0.0, str(path))

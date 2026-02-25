from __future__ import annotations

from typing import Any

from core.models import OCRRawResult
from ocr.base import OCRAdapterError

CUDA_UNAVAILABLE_HINTS = (
    "cuda",
    "gpu",
    "nvidia",
    "not available",
    "is unavailable",
    "not compiled",
    "no kernel image",
    "driver",
)


def _points_to_bbox(points: list[list[float]] | list[tuple[float, float]]) -> list[float]:
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


class YomitokuOCRAdapter:
    name = "yomitoku"
    version = "unknown"

    def __init__(self, device: str = "cuda", visualize: bool = False) -> None:
        self.device = device
        self.visualize = visualize
        self._cv2: Any = None
        self._np: Any = None
        self._ocr: Any = None
        self._ocr_cls: Any = None
        self._load_dependency()

    def _load_dependency(self) -> None:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
            import yomitoku  # type: ignore
            from yomitoku import OCR  # type: ignore
        except Exception as exc:
            raise OCRAdapterError(
                "yomitoku adapter requires `yomitoku` and `opencv-python`. "
                f"cause={exc}"
            ) from exc

        self._cv2 = cv2
        self._np = np
        self._ocr_cls = OCR
        self.version = str(getattr(yomitoku, "__version__", "unknown"))

    def _ensure_ocr(self) -> None:
        if self._ocr is not None:
            return
        requested_device = str(self.device or "cuda").strip().lower()
        if requested_device.startswith("cuda") and not self._is_cuda_available():
            self.device = "cpu"
        try:
            self._ocr = self._ocr_cls(device=self.device, visualize=self.visualize)
        except Exception as exc:
            if requested_device.startswith("cuda") and self._should_fallback_to_cpu(exc):
                try:
                    self.device = "cpu"
                    self._ocr = self._ocr_cls(device=self.device, visualize=self.visualize)
                    return
                except Exception as cpu_exc:
                    raise OCRAdapterError(
                        "failed to initialize yomitoku OCR on both CUDA and CPU: "
                        f"cuda_error={exc} cpu_error={cpu_exc}"
                    ) from cpu_exc
            raise OCRAdapterError(f"failed to initialize yomitoku OCR: {exc}") from exc

    def healthcheck(self) -> bool:
        return self._ocr_cls is not None

    def run(self, image_path: str) -> OCRRawResult:
        self._ensure_ocr()
        image = self._load_image(image_path)
        if image is None:
            raise OCRAdapterError(f"failed to load image for yomitoku: {image_path}")

        try:
            raw = self._ocr(image)
        except Exception as exc:
            raise OCRAdapterError(f"yomitoku OCR failed: {exc}") from exc

        lines = self._convert(raw)
        return OCRRawResult(
            engine=self.name,
            engine_version=self.version,
            payload=lines,
            metadata={"device": self.device},
        )

    def _load_image(self, image_path: str) -> Any:
        # cv2.imread can fail on Windows non-ASCII paths depending on build.
        image = self._cv2.imread(image_path)
        if image is not None:
            return image
        try:
            raw = self._np.fromfile(image_path, dtype=self._np.uint8)
        except Exception:
            return None
        if raw is None or getattr(raw, "size", 0) == 0:
            return None
        try:
            return self._cv2.imdecode(raw, self._cv2.IMREAD_COLOR)
        except Exception:
            return None

    def _convert(self, raw: Any) -> list[dict[str, Any]]:
        schema = raw
        if isinstance(raw, tuple) and raw:
            schema = raw[0]

        words = getattr(schema, "words", None)
        if words is None and isinstance(schema, dict):
            words = schema.get("words")
        if not isinstance(words, list):
            return []

        lines: list[dict[str, Any]] = []
        for idx, word in enumerate(words):
            content = self._get_attr(word, "content")
            if not content:
                continue
            points = self._get_attr(word, "points")
            polygon = self._normalize_points(points)
            if polygon is None:
                continue
            rec_score = self._as_float(self._get_attr(word, "rec_score"), default=0.0)
            det_score = self._as_float(self._get_attr(word, "det_score"), default=0.0)
            confidence = max(0.0, min(1.0, (rec_score + det_score) / 2))
            lines.append(
                {
                    "text": str(content).strip(),
                    "bbox": _points_to_bbox(polygon),
                    "polygon": polygon,
                    "confidence": confidence,
                    "line_index": idx,
                    "page": 1,
                }
            )
        return lines

    @staticmethod
    def _get_attr(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    @staticmethod
    def _normalize_points(value: Any) -> list[list[float]] | None:
        if not isinstance(value, list):
            return None
        points: list[list[float]] = []
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            try:
                x, y = float(item[0]), float(item[1])
            except Exception:
                continue
            points.append([x, y])
        return points if len(points) >= 4 else None

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _is_cuda_available() -> bool:
        try:
            import torch  # type: ignore
        except Exception:
            return False
        try:
            return bool(torch.cuda.is_available())
        except Exception:
            return False

    @staticmethod
    def _should_fallback_to_cpu(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(hint in message for hint in CUDA_UNAVAILABLE_HINTS)

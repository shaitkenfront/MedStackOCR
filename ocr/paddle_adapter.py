from __future__ import annotations

import os
from typing import Any

from core.models import OCRRawResult
from ocr.base import OCRAdapterError


def _polygon_to_bbox(polygon: list[list[float]] | list[tuple[float, float]]) -> list[float]:
    xs = [float(p[0]) for p in polygon]
    ys = [float(p[1]) for p in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


class PaddleOCRAdapter:
    name = "paddle"
    version = "unknown"

    def __init__(
        self,
        lang: str = "ja",
        use_gpu: bool = True,
        ocr_version: str | None = None,
    ) -> None:
        self.lang = lang
        self.use_gpu = use_gpu
        self.ocr_version = ocr_version
        self._paddleocr_module: Any = None
        self._ocr: Any = None
        self._load_dependency()

    def _load_dependency(self) -> None:
        try:
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            os.environ.setdefault("DISABLE_MODEL_SOURCE_CHECK", "True")
            import paddleocr  # type: ignore
            from paddleocr import PaddleOCR  # type: ignore
        except Exception as exc:
            raise OCRAdapterError(
                "paddle adapter requires `paddleocr` and `paddlepaddle`. "
                f"Install packages before using this engine. cause={exc}"
            ) from exc

        self._paddleocr_module = paddleocr
        self._paddle_ocr_cls = PaddleOCR
        paddleocr_version = getattr(paddleocr, "__version__", "unknown")
        self.version = f"paddleocr-{paddleocr_version}"

    def _ensure_ocr(self) -> None:
        if self._ocr is not None:
            return
        mapped_lang = self._map_lang(self.lang)
        device = "gpu:0" if self.use_gpu else "cpu"
        kwargs: dict[str, Any] = {
            "lang": mapped_lang,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "device": device,
            "enable_mkldnn": False,
        }
        if self.ocr_version:
            kwargs["ocr_version"] = self.ocr_version
        self._ocr = self._paddle_ocr_cls(**kwargs)

    @staticmethod
    def _map_lang(lang: str) -> str:
        if lang in {"ja", "jpn", "jp", "japan"}:
            return "japan"
        return lang

    def healthcheck(self) -> bool:
        return self._paddleocr_module is not None

    def run(self, image_path: str) -> OCRRawResult:
        self._ensure_ocr()
        try:
            if hasattr(self._ocr, "predict"):
                raw = self._ocr.predict(image_path)
            elif hasattr(self._ocr, "ocr"):
                # PaddleOCR 2.x uses `ocr()` while 3.x exposes `predict()`.
                raw = self._ocr.ocr(image_path, cls=False)
            else:
                raise OCRAdapterError("paddle OCR object has no usable inference method.")
        except NotImplementedError as exc:
            raise OCRAdapterError(
                "paddle runtime failed. This environment may be incompatible with current "
                "paddle/paddleocr build. Try different versions or GPU build."
            ) from exc
        except Exception as exc:
            raise OCRAdapterError(f"paddle OCR failed: {exc}") from exc

        lines = self._convert(raw)
        return OCRRawResult(
            engine=self.name,
            engine_version=self.version,
            payload=lines,
            metadata={
                "lang": self.lang,
                "use_gpu_requested": bool(self.use_gpu),
            },
        )

    def _convert(self, raw: Any) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        line_index = 0

        if isinstance(raw, list):
            for item in raw:
                line_index = self._extract_from_item(item, lines, line_index)
            return lines

        line_index = self._extract_from_item(raw, lines, line_index)
        return lines

    def _extract_from_item(self, item: Any, out: list[dict[str, Any]], start_idx: int) -> int:
        idx = start_idx
        if hasattr(item, "to_dict"):
            try:
                item = item.to_dict()
            except Exception:
                pass

        if isinstance(item, dict):
            rec_texts = self._get_list(item, ["rec_texts", "texts"])
            rec_scores = self._get_list(item, ["rec_scores", "scores"])
            polys = self._get_list(item, ["dt_polys", "polys", "boxes"])

            if rec_texts:
                for i, text in enumerate(rec_texts):
                    content = str(text).strip()
                    if not content:
                        continue
                    polygon = self._normalize_polygon(polys, i)
                    bbox = _polygon_to_bbox(polygon) if polygon else None
                    if bbox is None:
                        continue
                    score = self._get_score(rec_scores, i)
                    out.append(
                        {
                            "text": content,
                            "bbox": bbox,
                            "polygon": polygon,
                            "confidence": score,
                            "line_index": idx,
                            "page": 1,
                        }
                    )
                    idx += 1
                return idx

        # PaddleOCR v2 style: [[[x,y]...], (text, score)]
        if isinstance(item, list):
            for row in item:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                polygon_raw = row[0]
                rec_part = row[1]
                if not isinstance(polygon_raw, (list, tuple)) or not isinstance(rec_part, (list, tuple)):
                    continue
                if len(rec_part) < 1:
                    continue
                text = str(rec_part[0]).strip()
                if not text:
                    continue
                polygon = self._normalize_polygon([polygon_raw], 0)
                bbox = _polygon_to_bbox(polygon) if polygon else None
                if bbox is None:
                    continue
                score = 0.0
                if len(rec_part) > 1:
                    try:
                        score = float(rec_part[1])
                    except Exception:
                        score = 0.0
                out.append(
                    {
                        "text": text,
                        "bbox": bbox,
                        "polygon": polygon,
                        "confidence": max(0.0, min(1.0, score)),
                        "line_index": idx,
                        "page": 1,
                    }
                )
                idx += 1
        return idx

    @staticmethod
    def _get_list(data: dict[str, Any], keys: list[str]) -> list[Any]:
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, tuple):
                return list(value)
            if hasattr(value, "tolist"):
                converted = value.tolist()
                if isinstance(converted, list):
                    return converted
        return []

    @staticmethod
    def _get_score(scores: list[Any], index: int) -> float:
        if 0 <= index < len(scores):
            try:
                value = float(scores[index])
            except Exception:
                return 0.0
            if value > 1.0:
                value /= 100.0
            return max(0.0, min(1.0, value))
        return 0.0

    @staticmethod
    def _normalize_polygon(polys: list[Any], index: int) -> list[list[float]] | None:
        if not (0 <= index < len(polys)):
            return None
        raw = polys[index]
        if hasattr(raw, "tolist"):
            raw = raw.tolist()
        if not isinstance(raw, (list, tuple)):
            return None
        points: list[list[float]] = []
        for point in raw:
            if hasattr(point, "tolist"):
                point = point.tolist()
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                continue
            try:
                x = float(point[0])
                y = float(point[1])
            except Exception:
                continue
            points.append([x, y])
        return points if len(points) >= 4 else None

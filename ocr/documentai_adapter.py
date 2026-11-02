from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.models import OCRRawResult
from ocr.base import OCRAdapterError


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


class GoogleDocumentAIAdapter:
    name = "documentai"
    version = "unknown"

    def __init__(
        self,
        project_id: str | None = None,
        location: str = "us",
        processor_id: str | None = None,
        processor_version: str | None = None,
        endpoint: str | None = None,
        credentials_path: str | None = None,
        timeout_sec: int = 120,
        mime_type: str | None = None,
        field_mask: str | None = None,
    ) -> None:
        self.project_id = (project_id or "").strip()
        self.location = (location or "us").strip()
        self.processor_id = (processor_id or "").strip()
        self.processor_version = (processor_version or "").strip()
        self.endpoint = (endpoint or "").strip()
        self.credentials_path = (credentials_path or "").strip()
        self.timeout_sec = int(timeout_sec)
        self.mime_type = (mime_type or "").strip() or None
        self.field_mask = (field_mask or "").strip() or None
        self._documentai: Any = None
        self._client_options_cls: Any = None
        self._client: Any = None
        self._processor_name = self._build_processor_name()
        self._load_dependency()
        self._configure_credentials()

    def _load_dependency(self) -> None:
        try:
            from google.api_core.client_options import ClientOptions  # type: ignore
            from google.cloud import documentai  # type: ignore
        except Exception as exc:
            raise OCRAdapterError(
                "documentai adapter requires `google-cloud-documentai` package."
            ) from exc

        self._documentai = documentai
        self._client_options_cls = ClientOptions
        package_version = getattr(documentai, "__version__", "unknown")
        self.version = f"google-documentai-{package_version}"

    def _configure_credentials(self) -> None:
        if self.credentials_path:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", self.credentials_path)

    def _build_processor_name(self) -> str:
        if not self.project_id or not self.processor_id:
            return ""
        base = (
            f"projects/{self.project_id}/locations/{self.location}/processors/{self.processor_id}"
        )
        if self.processor_version:
            return f"{base}/processorVersions/{self.processor_version}"
        return base

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        if self._documentai is None or self._client_options_cls is None:
            raise OCRAdapterError("documentai adapter dependency is not initialized.")
        if not self._processor_name:
            raise OCRAdapterError(
                "documentai requires `project_id` and `processor_id` in config."
            )

        api_endpoint = self.endpoint or f"{self.location}-documentai.googleapis.com"
        options = self._client_options_cls(api_endpoint=api_endpoint)
        self._client = self._documentai.DocumentProcessorServiceClient(client_options=options)

    def healthcheck(self) -> bool:
        return bool(self._documentai is not None and self._processor_name)

    def run(self, image_path: str) -> OCRRawResult:
        self._ensure_client()
        image = Path(image_path)
        if not image.exists():
            raise OCRAdapterError(f"input image not found: {image_path}")

        content = image.read_bytes()
        mime_type = self.mime_type or self._detect_mime_type(image)

        try:
            raw_document = self._documentai.RawDocument(content=content, mime_type=mime_type)
            request_kwargs: dict[str, Any] = {
                "name": self._processor_name,
                "raw_document": raw_document,
            }
            if self.field_mask:
                request_kwargs["field_mask"] = self.field_mask
            request = self._documentai.ProcessRequest(**request_kwargs)
            result = self._client.process_document(request=request, timeout=self.timeout_sec)
        except Exception as exc:
            raise OCRAdapterError(f"documentai OCR failed: {exc}") from exc

        document = getattr(result, "document", None)
        lines = self._convert_document(document)
        return OCRRawResult(
            engine=self.name,
            engine_version=self.version,
            payload=lines,
            metadata={
                "processor_name": self._processor_name,
                "location": self.location,
                "mime_type": mime_type,
            },
        )

    @staticmethod
    def _detect_mime_type(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return "application/pdf"
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suffix == ".png":
            return "image/png"
        if suffix in {".tif", ".tiff"}:
            return "image/tiff"
        if suffix == ".bmp":
            return "image/bmp"
        return "application/octet-stream"

    def _convert_document(self, document: Any) -> list[dict[str, Any]]:
        if document is None:
            return []

        full_text = str(getattr(document, "text", "") or "")
        pages = self._to_list(getattr(document, "pages", None))
        if not pages:
            return []

        normalized: list[dict[str, Any]] = []
        line_index = 0
        for page_no, page in enumerate(pages, start=1):
            items = self._pick_layout_items(page)
            for item in items:
                layout = getattr(item, "layout", None)
                text = self._extract_text(full_text, layout)
                if not text:
                    continue
                bbox, polygon = self._extract_geometry(layout)
                confidence = self._extract_confidence(item, layout)

                row: dict[str, Any] = {
                    "text": text,
                    "bbox": bbox,
                    "confidence": confidence,
                    "line_index": line_index,
                    "page": page_no,
                }
                if polygon is not None:
                    row["polygon"] = polygon
                normalized.append(row)
                line_index += 1
        return normalized

    @staticmethod
    def _pick_layout_items(page: Any) -> list[Any]:
        lines = GoogleDocumentAIAdapter._to_list(getattr(page, "lines", None))
        if lines:
            return lines
        tokens = GoogleDocumentAIAdapter._to_list(getattr(page, "tokens", None))
        if tokens:
            return tokens
        return []

    @staticmethod
    def _extract_text(full_text: str, layout: Any) -> str:
        if layout is None:
            return ""
        text_anchor = getattr(layout, "text_anchor", None)
        if text_anchor is None:
            return ""
        segments = GoogleDocumentAIAdapter._to_list(getattr(text_anchor, "text_segments", None))
        if not segments:
            return ""

        chunks: list[str] = []
        for seg in segments:
            start = getattr(seg, "start_index", 0) or 0
            end = getattr(seg, "end_index", 0) or 0
            try:
                s = int(start)
                e = int(end)
            except (TypeError, ValueError):
                continue
            if e <= s:
                continue
            chunks.append(full_text[s:e])

        if not chunks:
            return ""
        text = "".join(chunks).strip()
        return " ".join(text.split())

    @staticmethod
    def _extract_geometry(layout: Any) -> tuple[list[float], list[list[float]] | None]:
        if layout is None:
            return [0.0, 0.0, 1.0, 1.0], None

        poly = getattr(layout, "bounding_poly", None)
        if poly is None:
            return [0.0, 0.0, 1.0, 1.0], None

        normalized_vertices = getattr(poly, "normalized_vertices", None)
        polygon = GoogleDocumentAIAdapter._to_polygon(normalized_vertices)
        if polygon:
            return GoogleDocumentAIAdapter._polygon_to_bbox(polygon), polygon

        vertices = getattr(poly, "vertices", None)
        polygon = GoogleDocumentAIAdapter._to_polygon(vertices)
        if polygon:
            return GoogleDocumentAIAdapter._polygon_to_bbox(polygon), polygon

        return [0.0, 0.0, 1.0, 1.0], None

    @staticmethod
    def _to_polygon(vertices: Any) -> list[list[float]] | None:
        points_input = GoogleDocumentAIAdapter._to_list(vertices)
        if not points_input:
            return None
        points: list[list[float]] = []
        for vertex in points_input:
            x = getattr(vertex, "x", None)
            y = getattr(vertex, "y", None)
            try:
                fx = float(x)
                fy = float(y)
            except (TypeError, ValueError):
                continue
            points.append([fx, fy])
        if len(points) < 3:
            return None
        return points

    @staticmethod
    def _to_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        try:
            return list(value)
        except TypeError:
            return []

    @staticmethod
    def _polygon_to_bbox(polygon: list[list[float]]) -> list[float]:
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        return [min(xs), min(ys), max(xs), max(ys)]

    @staticmethod
    def _extract_confidence(item: Any, layout: Any) -> float:
        for obj in (item, layout):
            if obj is None:
                continue
            value = getattr(obj, "confidence", None)
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

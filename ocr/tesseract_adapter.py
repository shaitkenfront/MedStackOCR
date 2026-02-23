from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.models import OCRRawResult
from ocr.base import OCRAdapterError


class TesseractAdapter:
    name = "tesseract"
    version = "unknown"

    def __init__(
        self,
        lang: str = "jpn",
        tesseract_cmd: str | None = None,
        tessdata_dir: str | None = None,
    ) -> None:
        self.lang = lang
        self.tesseract_cmd = tesseract_cmd
        self.tessdata_dir = tessdata_dir
        self._pytesseract: Any = None
        self._image_module: Any = None
        self._load_dependency()

    def _load_dependency(self) -> None:
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore
        except Exception as exc:
            raise OCRAdapterError(
                "tesseract adapter requires pytesseract and pillow. "
                "Install dependencies and Tesseract OCR binary."
            ) from exc
        self._pytesseract = pytesseract
        self._image_module = Image
        self._configure_executable()
        self._configure_tessdata()

        try:
            self.version = str(pytesseract.get_tesseract_version())
        except Exception:
            self.version = "unknown"

    def _configure_executable(self) -> None:
        if self._pytesseract is None:
            return

        candidate_paths: list[str] = []
        if self.tesseract_cmd:
            candidate_paths.append(self.tesseract_cmd)
        candidate_paths.append(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        candidate_paths.append(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")

        for path in candidate_paths:
            if path and Path(path).exists():
                self._pytesseract.pytesseract.tesseract_cmd = path
                return

    def _configure_tessdata(self) -> None:
        if self.tessdata_dir:
            os.environ["TESSDATA_PREFIX"] = self.tessdata_dir
            return

        default = Path(r"C:\Program Files\Tesseract-OCR\tessdata")
        if default.exists():
            os.environ.setdefault("TESSDATA_PREFIX", str(default))

    def healthcheck(self) -> bool:
        if self._pytesseract is None:
            return False
        try:
            _ = self._pytesseract.get_tesseract_version()
            return True
        except Exception:
            return False

    def run(self, image_path: str) -> OCRRawResult:
        if self._pytesseract is None or self._image_module is None:
            raise OCRAdapterError("tesseract dependency is not available.")

        image = self._image_module.open(image_path)
        data = self._pytesseract.image_to_data(
            image,
            lang=self.lang,
            output_type=self._pytesseract.Output.DICT,
        )
        payload = self._to_lines(data)
        return OCRRawResult(
            engine=self.name,
            engine_version=self.version,
            payload=payload,
            metadata={"lang": self.lang},
        )

    @staticmethod
    def _to_lines(data: dict[str, list[Any]]) -> list[dict[str, Any]]:
        lines: dict[tuple[int, int, int, int], dict[str, Any]] = {}
        count = len(data.get("text", []))

        for i in range(count):
            text = str(data["text"][i]).strip()
            if not text:
                continue

            key = (
                int(data.get("page_num", [1] * count)[i]),
                int(data.get("block_num", [0] * count)[i]),
                int(data.get("par_num", [0] * count)[i]),
                int(data.get("line_num", [i] * count)[i]),
            )
            left = int(data.get("left", [0] * count)[i])
            top = int(data.get("top", [0] * count)[i])
            width = int(data.get("width", [0] * count)[i])
            height = int(data.get("height", [0] * count)[i])
            conf_raw = data.get("conf", ["0"] * count)[i]
            try:
                conf = max(0.0, float(conf_raw))
            except (TypeError, ValueError):
                conf = 0.0

            if key not in lines:
                lines[key] = {
                    "text_parts": [],
                    "x1": left,
                    "y1": top,
                    "x2": left + width,
                    "y2": top + height,
                    "confidence_sum": 0.0,
                    "confidence_count": 0,
                    "page": key[0],
                }

            entry = lines[key]
            entry["text_parts"].append(text)
            entry["x1"] = min(entry["x1"], left)
            entry["y1"] = min(entry["y1"], top)
            entry["x2"] = max(entry["x2"], left + width)
            entry["y2"] = max(entry["y2"], top + height)
            entry["confidence_sum"] += conf
            entry["confidence_count"] += 1

        normalized_lines: list[dict[str, Any]] = []
        for index, (_, row) in enumerate(sorted(lines.items())):
            confidence_count = max(1, int(row["confidence_count"]))
            normalized_lines.append(
                {
                    "text": " ".join(row["text_parts"]).strip(),
                    "bbox": [row["x1"], row["y1"], row["x2"], row["y2"]],
                    "confidence": row["confidence_sum"] / confidence_count / 100.0,
                    "line_index": index,
                    "page": row["page"],
                }
            )
        return normalized_lines

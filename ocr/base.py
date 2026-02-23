from __future__ import annotations

from typing import Protocol

from core.models import OCRRawResult


class OCRAdapter(Protocol):
    name: str
    version: str

    def run(self, image_path: str) -> OCRRawResult:
        ...

    def healthcheck(self) -> bool:
        ...


class OCRAdapterError(RuntimeError):
    pass


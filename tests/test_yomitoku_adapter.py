from __future__ import annotations

import unittest

from ocr.base import OCRAdapterError
from ocr.yomitoku_adapter import YomitokuOCRAdapter


class _TestableYomitokuAdapter(YomitokuOCRAdapter):
    def __init__(self, *, ocr_cls: type, device: str = "cuda", cuda_available: bool = True) -> None:
        self._test_ocr_cls = ocr_cls
        self._test_cuda_available = cuda_available
        super().__init__(device=device, visualize=False)

    def _load_dependency(self) -> None:
        self._cv2 = object()
        self._np = object()
        self._ocr_cls = self._test_ocr_cls
        self.version = "test"

    def _is_cuda_available(self) -> bool:  # type: ignore[override]
        return self._test_cuda_available


class YomitokuAdapterTest(unittest.TestCase):
    def test_fallback_to_cpu_when_cuda_is_unavailable(self) -> None:
        calls: list[str] = []

        class RecordingOCR:
            def __init__(self, *, device: str, visualize: bool) -> None:
                _ = visualize
                calls.append(device)

        adapter = _TestableYomitokuAdapter(ocr_cls=RecordingOCR, device="cuda", cuda_available=False)
        adapter._ensure_ocr()  # noqa: SLF001

        self.assertEqual(adapter.device, "cpu")
        self.assertEqual(calls, ["cpu"])

    def test_fallback_to_cpu_when_cuda_init_fails(self) -> None:
        calls: list[str] = []

        class FallbackOCR:
            def __init__(self, *, device: str, visualize: bool) -> None:
                _ = visualize
                calls.append(device)
                if device.startswith("cuda"):
                    raise RuntimeError("CUDA is unavailable on this machine")

        adapter = _TestableYomitokuAdapter(ocr_cls=FallbackOCR, device="cuda", cuda_available=True)
        adapter._ensure_ocr()  # noqa: SLF001

        self.assertEqual(adapter.device, "cpu")
        self.assertEqual(calls, ["cuda", "cpu"])

    def test_raise_error_for_non_cuda_failure(self) -> None:
        calls: list[str] = []

        class ErrorOCR:
            def __init__(self, *, device: str, visualize: bool) -> None:
                _ = visualize
                calls.append(device)
                raise RuntimeError("model file missing")

        adapter = _TestableYomitokuAdapter(ocr_cls=ErrorOCR, device="cuda", cuda_available=True)
        with self.assertRaises(OCRAdapterError):
            adapter._ensure_ocr()  # noqa: SLF001

        self.assertEqual(calls, ["cuda"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ocr.documentai_adapter import GoogleDocumentAIAdapter


class _FakeTextSegment:
    def __init__(self, start_index: int, end_index: int) -> None:
        self.start_index = start_index
        self.end_index = end_index


class _FakeTextAnchor:
    def __init__(self, text_segments: list[_FakeTextSegment]) -> None:
        self.text_segments = text_segments


class _FakeVertex:
    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y


class _FakeBoundingPoly:
    def __init__(self, normalized_vertices: list[_FakeVertex]) -> None:
        self.normalized_vertices = normalized_vertices
        self.vertices: list[_FakeVertex] = []


class _FakeLayout:
    def __init__(self, text_anchor: _FakeTextAnchor, bounding_poly: _FakeBoundingPoly) -> None:
        self.text_anchor = text_anchor
        self.bounding_poly = bounding_poly
        self.confidence = 0.88


class _FakeLine:
    def __init__(self, layout: _FakeLayout, confidence: float = 0.9) -> None:
        self.layout = layout
        self.confidence = confidence


class _FakePage:
    def __init__(self, lines: list[_FakeLine]) -> None:
        self.lines = lines


class _FakeDocument:
    def __init__(self, text: str, pages: list[_FakePage]) -> None:
        self.text = text
        self.pages = pages


class _FakeProcessResponse:
    def __init__(self, document: _FakeDocument) -> None:
        self.document = document


class _FakeRawDocument:
    def __init__(self, content: bytes, mime_type: str) -> None:
        self.content = content
        self.mime_type = mime_type


class _FakeProcessRequest:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _FakeClient:
    def __init__(self, client_options: object | None = None) -> None:
        self.client_options = client_options
        self.last_request: object | None = None
        self.last_timeout: int | None = None

    def process_document(self, request: object, timeout: int | None = None) -> _FakeProcessResponse:
        self.last_request = request
        self.last_timeout = timeout
        text = "hello world"
        line = _FakeLine(
            _FakeLayout(
                text_anchor=_FakeTextAnchor([_FakeTextSegment(0, 11)]),
                bounding_poly=_FakeBoundingPoly(
                    [
                        _FakeVertex(0.1, 0.2),
                        _FakeVertex(0.1, 0.3),
                        _FakeVertex(0.4, 0.3),
                        _FakeVertex(0.4, 0.2),
                    ]
                ),
            )
        )
        return _FakeProcessResponse(_FakeDocument(text=text, pages=[_FakePage([line])]))


class _FakeClientOptions:
    def __init__(self, api_endpoint: str | None = None) -> None:
        self.api_endpoint = api_endpoint


class DocumentAIAdapterTest(unittest.TestCase):
    def test_healthcheck_false_when_processor_missing(self) -> None:
        modules = _build_fake_google_modules()
        with mock.patch.dict(sys.modules, modules):
            adapter = GoogleDocumentAIAdapter(project_id="", processor_id="")
        self.assertFalse(adapter.healthcheck())

    def test_run_success(self) -> None:
        modules = _build_fake_google_modules()
        with tempfile.TemporaryDirectory() as tmp_dir:
            image = Path(tmp_dir) / "sample.jpg"
            image.write_bytes(b"fake-image")

            with mock.patch.dict(sys.modules, modules):
                adapter = GoogleDocumentAIAdapter(
                    project_id="demo-project",
                    location="us",
                    processor_id="processor-1",
                    timeout_sec=30,
                )
                self.assertTrue(adapter.healthcheck())
                result = adapter.run(str(image))

        self.assertEqual(result.engine, "documentai")
        self.assertEqual(result.metadata["mime_type"], "image/jpeg")
        self.assertEqual(len(result.payload), 1)
        self.assertEqual(result.payload[0]["text"], "hello world")
        self.assertAlmostEqual(float(result.payload[0]["confidence"]), 0.9, places=3)
        self.assertEqual(result.payload[0]["bbox"], [0.1, 0.2, 0.4, 0.3])


def _build_fake_google_modules() -> dict[str, types.ModuleType]:
    google_module = types.ModuleType("google")
    cloud_module = types.ModuleType("google.cloud")
    documentai_module = types.ModuleType("google.cloud.documentai")
    api_core_module = types.ModuleType("google.api_core")
    client_options_module = types.ModuleType("google.api_core.client_options")

    documentai_module.DocumentProcessorServiceClient = _FakeClient
    documentai_module.RawDocument = _FakeRawDocument
    documentai_module.ProcessRequest = _FakeProcessRequest
    documentai_module.__version__ = "test"

    client_options_module.ClientOptions = _FakeClientOptions

    cloud_module.documentai = documentai_module
    api_core_module.client_options = client_options_module
    google_module.cloud = cloud_module
    google_module.api_core = api_core_module

    return {
        "google": google_module,
        "google.cloud": cloud_module,
        "google.cloud.documentai": documentai_module,
        "google.api_core": api_core_module,
        "google.api_core.client_options": client_options_module,
    }


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from ocr.factory import create_ocr_adapter
from ocr.documentai_adapter import GoogleDocumentAIAdapter
from ocr.ndlocr_lite_adapter import NdlOcrLiteAdapter


class OCRFactoryTest(unittest.TestCase):
    def test_create_ndlocr_lite_adapter(self) -> None:
        config = {"ocr": {"engines": {"ndlocr_lite": {"command": "python -V"}}}}
        adapter = create_ocr_adapter("ndlocr-lite", config)
        self.assertIsInstance(adapter, NdlOcrLiteAdapter)

    def test_create_documentai_adapter(self) -> None:
        config = {
            "ocr": {
                "engines": {
                    "documentai": {
                        "project_id": "demo-project",
                        "location": "us",
                        "processor_id": "processor-1",
                    }
                }
            }
        }
        with mock.patch.dict(sys.modules, _fake_google_modules()):
            adapter = create_ocr_adapter("documentai", config)
        self.assertIsInstance(adapter, GoogleDocumentAIAdapter)


def _fake_google_modules() -> dict[str, types.ModuleType]:
    google_module = types.ModuleType("google")
    cloud_module = types.ModuleType("google.cloud")
    documentai_module = types.ModuleType("google.cloud.documentai")
    api_core_module = types.ModuleType("google.api_core")
    client_options_module = types.ModuleType("google.api_core.client_options")

    documentai_module.DocumentProcessorServiceClient = object
    documentai_module.RawDocument = object
    documentai_module.ProcessRequest = object
    documentai_module.__version__ = "test"

    class _ClientOptions:
        def __init__(self, api_endpoint: str | None = None) -> None:
            self.api_endpoint = api_endpoint

    client_options_module.ClientOptions = _ClientOptions
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

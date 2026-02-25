from __future__ import annotations

import sys
import types
import unittest
from copy import deepcopy
from unittest import mock

from app.config import DEFAULT_CONFIG
from ocr.base import OCRAdapterError
from ocr.factory import create_ocr_adapter


class OCRFactoryTest(unittest.TestCase):
    def test_disallow_non_locked_engine(self) -> None:
        with self.assertRaises(OCRAdapterError):
            create_ocr_adapter("mock", deepcopy(DEFAULT_CONFIG))

    def test_allow_configured_engine(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        config["ocr"]["engine"] = "mock"
        config["ocr"]["allowed_engines"] = ["mock"]
        config["ocr"]["engines"]["mock"]["enabled"] = True
        adapter = create_ocr_adapter("mock", config)
        self.assertEqual(type(adapter).__name__, "MockOCRAdapter")

    def test_allow_documentai_alias_engine(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        config["ocr"]["engine"] = "documentai"
        config["ocr"]["allowed_engines"] = ["documentai"]
        config["ocr"]["engines"]["documentai"]["enabled"] = True
        config["ocr"]["engines"]["documentai"]["project_id"] = "demo-project"
        config["ocr"]["engines"]["documentai"]["processor_id"] = "processor-1"
        with mock.patch.dict(sys.modules, _fake_google_modules()):
            adapter = create_ocr_adapter("document_ai", config)
        self.assertEqual(type(adapter).__name__, "GoogleDocumentAIAdapter")


def _fake_google_modules() -> dict[str, types.ModuleType]:
    google_module = types.ModuleType("google")
    cloud_module = types.ModuleType("google.cloud")
    documentai_module = types.ModuleType("google.cloud.documentai")
    api_core_module = types.ModuleType("google.api_core")
    client_options_module = types.ModuleType("google.api_core.client_options")

    class _ClientOptions:
        def __init__(self, api_endpoint: str | None = None) -> None:
            self.api_endpoint = api_endpoint

    documentai_module.DocumentProcessorServiceClient = object
    documentai_module.RawDocument = object
    documentai_module.ProcessRequest = object
    documentai_module.__version__ = "test"
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

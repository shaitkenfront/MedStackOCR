from __future__ import annotations

import unittest
from copy import deepcopy

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


if __name__ == "__main__":
    unittest.main()

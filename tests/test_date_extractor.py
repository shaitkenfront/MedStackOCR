from __future__ import annotations

import unittest

from core.models import OCRLine
from extractors.date_extractor import DateExtractor


class DateExtractorTest(unittest.TestCase):
    def test_extract_gregorian_date(self) -> None:
        extractor = DateExtractor()
        lines = [
            OCRLine(
                text="領収日 2026/02/22",
                bbox=(0.5, 0.2, 0.9, 0.25),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            )
        ]
        candidates = extractor.extract(lines)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].value_normalized, "2026-02-22")

    def test_extract_japanese_era_date(self) -> None:
        extractor = DateExtractor()
        lines = [
            OCRLine(
                text="発行日 令和8年2月22日",
                bbox=(0.5, 0.2, 0.9, 0.25),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            )
        ]
        candidates = extractor.extract(lines)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].value_normalized, "2026-02-22")


if __name__ == "__main__":
    unittest.main()


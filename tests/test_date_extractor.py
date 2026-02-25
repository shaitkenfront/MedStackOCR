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

    def test_extract_japanese_era_date_with_spaces(self) -> None:
        extractor = DateExtractor()
        lines = [
            OCRLine(
                text="発行日 令和 8年 1月16日",
                bbox=(0.5, 0.2, 0.9, 0.25),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            )
        ]
        candidates = extractor.extract(lines)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].value_normalized, "2026-01-16")

    def test_extract_date_with_nearby_label_line(self) -> None:
        extractor = DateExtractor()
        lines = [
            OCRLine(
                text="発行日",
                bbox=(0.46, 0.46, 0.50, 0.49),
                polygon=None,
                confidence=0.95,
                line_index=10,
                page=1,
            ),
            OCRLine(
                text="令和8年1月23日",
                bbox=(0.53, 0.46, 0.62, 0.49),
                polygon=None,
                confidence=0.66,
                line_index=11,
                page=1,
            ),
        ]
        candidates = extractor.extract(lines)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].value_normalized, "2026-01-23")
        self.assertIn("near_preferred_date_label", candidates[0].reasons)
        self.assertIn(10, candidates[0].source_line_indices)

    def test_extract_era_without_marker_ymd_formats(self) -> None:
        extractor = DateExtractor()
        samples = ("発行日 8/1/23", "発行日 8-1-23", "発行日 8.1.23")
        for idx, text in enumerate(samples):
            with self.subTest(text=text):
                lines = [
                    OCRLine(
                        text=text,
                        bbox=(0.5, 0.2 + idx * 0.05, 0.9, 0.25 + idx * 0.05),
                        polygon=None,
                        confidence=0.95,
                        line_index=idx,
                        page=1,
                    )
                ]
                candidates = extractor.extract(lines)
                self.assertTrue(candidates)
                self.assertEqual(candidates[0].value_normalized, "2026-01-23")


if __name__ == "__main__":
    unittest.main()

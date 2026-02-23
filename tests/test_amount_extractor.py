from __future__ import annotations

import unittest

from core.models import OCRLine
from extractors.amount_extractor import AmountExtractor


class AmountExtractorTest(unittest.TestCase):
    def test_extract_amount_prefers_payment_context(self) -> None:
        extractor = AmountExtractor()
        lines = [
            OCRLine(
                text="総点数 1840点",
                bbox=(0.3, 0.7, 0.7, 0.75),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="今回お支払額 ¥1,840",
                bbox=(0.55, 0.85, 0.95, 0.92),
                polygon=None,
                confidence=0.95,
                line_index=1,
                page=1,
            ),
        ]
        candidates = extractor.extract(lines)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].value_normalized, 1840)
        self.assertIn("has_primary_amount_label", candidates[0].reasons)

    def test_extract_amount_ignores_phone_number_context(self) -> None:
        extractor = AmountExtractor()
        lines = [
            OCRLine(
                text="TEL : 03-0000-0000",
                bbox=(0.4, 0.7, 0.9, 0.75),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="請求金額",
                bbox=(0.45, 0.82, 0.6, 0.86),
                polygon=None,
                confidence=0.95,
                line_index=1,
                page=1,
            ),
            OCRLine(
                text="1,190円",
                bbox=(0.65, 0.82, 0.9, 0.86),
                polygon=None,
                confidence=0.95,
                line_index=2,
                page=1,
            ),
        ]
        candidates = extractor.extract(lines)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].value_normalized, 1190)
        self.assertIn("has_currency_marker", candidates[0].reasons)

    def test_extract_amount_penalizes_year_like_number(self) -> None:
        extractor = AmountExtractor()
        lines = [
            OCRLine(
                text="発行日 2026/01/09",
                bbox=(0.4, 0.2, 0.8, 0.25),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="領収金額 3,200円",
                bbox=(0.4, 0.8, 0.9, 0.85),
                polygon=None,
                confidence=0.95,
                line_index=1,
                page=1,
            ),
        ]
        candidates = extractor.extract(lines)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].value_normalized, 3200)

    def test_extract_amount_excludes_slip_number_context(self) -> None:
        extractor = AmountExtractor()
        lines = [
            OCRLine(
                text="診療費請求書兼領収書",
                bbox=(0.25, 0.09, 0.45, 0.12),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="No. 43201",
                bbox=(0.09, 0.09, 0.16, 0.12),
                polygon=None,
                confidence=0.95,
                line_index=1,
                page=1,
            ),
            OCRLine(
                text="今回請求額",
                bbox=(0.40, 0.79, 0.47, 0.81),
                polygon=None,
                confidence=0.95,
                line_index=2,
                page=1,
            ),
            OCRLine(
                text="440円",
                bbox=(0.58, 0.79, 0.64, 0.81),
                polygon=None,
                confidence=0.95,
                line_index=3,
                page=1,
            ),
        ]
        candidates = extractor.extract(lines)
        self.assertTrue(candidates)
        self.assertTrue(all(c.value_normalized != 43201 for c in candidates))
        self.assertEqual(candidates[0].value_normalized, 440)

    def test_near_primary_amount_label_requires_amount_suffix(self) -> None:
        extractor = AmountExtractor()
        lines = [
            OCRLine(
                text="※ 領収書は「医療費控除」等の提出に必要です。",
                bbox=(0.02, 0.86, 0.35, 0.88),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="※ 万が一請求漏れ等が生じた場合、後日に請求させていただきます。",
                bbox=(0.02, 0.85, 0.33, 0.87),
                polygon=None,
                confidence=0.95,
                line_index=1,
                page=1,
            ),
            OCRLine(
                text="10円",
                bbox=(0.12, 0.86, 0.18, 0.88),
                polygon=None,
                confidence=0.95,
                line_index=2,
                page=1,
            ),
        ]
        candidates = extractor.extract(lines)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].value_normalized, 10)
        self.assertNotIn("near_primary_amount_label", candidates[0].reasons)

    def test_extract_amount_ignores_negative_amounts(self) -> None:
        extractor = AmountExtractor()
        lines = [
            OCRLine(
                text="返金 -1,000円",
                bbox=(0.20, 0.70, 0.45, 0.75),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="調整額 (2,000)",
                bbox=(0.20, 0.76, 0.45, 0.81),
                polygon=None,
                confidence=0.95,
                line_index=1,
                page=1,
            ),
            OCRLine(
                text="今回請求額 3,000円",
                bbox=(0.20, 0.82, 0.55, 0.88),
                polygon=None,
                confidence=0.95,
                line_index=2,
                page=1,
            ),
        ]
        candidates = extractor.extract(lines)
        self.assertTrue(candidates)
        self.assertTrue(all(c.value_normalized not in (1000, 2000) for c in candidates))
        self.assertEqual(candidates[0].value_normalized, 3000)

    def test_extract_amount_applies_text_height_bonus_and_penalty(self) -> None:
        extractor = AmountExtractor()
        lines = [
            OCRLine(
                text="今回請求額 2,000円",
                bbox=(0.20, 0.80, 0.55, 0.83),
                polygon=None,
                confidence=0.95,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="今回請求額 2,100円",
                bbox=(0.20, 0.84, 0.55, 0.85),
                polygon=None,
                confidence=0.95,
                line_index=1,
                page=1,
            ),
        ]
        candidates = extractor.extract(lines)
        self.assertEqual(len(candidates), 2)

        large_text_candidate = next(c for c in candidates if c.value_normalized == 2000)
        small_text_candidate = next(c for c in candidates if c.value_normalized == 2100)
        self.assertIn("large_text_bonus", large_text_candidate.reasons)
        self.assertIn("small_text_penalty", small_text_candidate.reasons)
        self.assertGreater(large_text_candidate.score, small_text_candidate.score)


if __name__ == "__main__":
    unittest.main()

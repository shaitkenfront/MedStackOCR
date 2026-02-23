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


if __name__ == "__main__":
    unittest.main()


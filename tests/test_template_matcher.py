from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.models import OCRLine
from templates.matcher import TemplateMatcher
from templates.store import TemplateStore


class TemplateMatcherTest(unittest.TestCase):
    def test_match_and_apply_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TemplateStore(tmp)
            template = {
                "template_family_id": "pharmacy_family_001",
                "scope": "household",
                "household_id": "household_demo",
                "document_type": "pharmacy",
                "anchors": [
                    {"text_pattern": "領収書", "bbox": [0.4, 0.02, 0.62, 0.07]},
                    {"text_pattern": "TEL", "bbox": [0.05, 0.10, 0.20, 0.14]},
                ],
                "field_specs": {
                    "payment_amount": {
                        "target_bbox": [0.45, 0.70, 0.98, 0.98],
                        "selection_rules": ["prefer_label:領収,請求,お支払,合計", "parse_amount"],
                    }
                },
                "sample_count": 1,
                "success_rate": 1.0,
            }
            store.save_template(template)
            matcher = TemplateMatcher(store, match_threshold=0.5)

            lines = [
                OCRLine(
                    text="領収書",
                    bbox=(0.45, 0.03, 0.55, 0.06),
                    polygon=None,
                    confidence=0.95,
                    line_index=0,
                    page=1,
                ),
                OCRLine(
                    text="TEL 03-1234-5678",
                    bbox=(0.06, 0.11, 0.20, 0.14),
                    polygon=None,
                    confidence=0.93,
                    line_index=1,
                    page=1,
                ),
                OCRLine(
                    text="今回お支払額 1,840円",
                    bbox=(0.60, 0.88, 0.95, 0.93),
                    polygon=None,
                    confidence=0.94,
                    line_index=2,
                    page=1,
                ),
            ]
            match, matched_template = matcher.match("household_demo", "pharmacy", lines)
            self.assertTrue(match.matched)
            self.assertIsNotNone(matched_template)

            candidates = matcher.apply_template(matched_template, lines)
            self.assertIn("payment_amount", candidates)
            self.assertTrue(candidates["payment_amount"])
            self.assertEqual(candidates["payment_amount"][0].value_normalized, 1840)


if __name__ == "__main__":
    unittest.main()


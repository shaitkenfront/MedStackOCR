from __future__ import annotations

import unittest

from classify.document_classifier import DocumentClassifier
from core.enums import DocumentType
from core.models import OCRLine


class DocumentClassifierTest(unittest.TestCase):
    def test_classify_pharmacy(self) -> None:
        classifier = DocumentClassifier()
        lines = [
            OCRLine(
                text="〇〇調剤薬局",
                bbox=(0.1, 0.05, 0.6, 0.1),
                polygon=None,
                confidence=0.9,
                line_index=0,
                page=1,
            ),
            OCRLine(
                text="処方箋交付医療機関 △△内科クリニック",
                bbox=(0.1, 0.35, 0.9, 0.4),
                polygon=None,
                confidence=0.9,
                line_index=1,
                page=1,
            ),
        ]
        doc_type, _, _, _ = classifier.classify(lines)
        self.assertEqual(doc_type, DocumentType.PHARMACY)

    def test_classify_unknown_on_empty(self) -> None:
        classifier = DocumentClassifier()
        doc_type, _, _, _ = classifier.classify([])
        self.assertEqual(doc_type, DocumentType.UNKNOWN)


if __name__ == "__main__":
    unittest.main()


from __future__ import annotations

import unittest

from core.enums import FieldName
from core.models import OCRLine
from extractors.family_name_extractor import FamilyNameExtractor, FamilyRegistryError


def _line(text: str, idx: int = 0) -> OCRLine:
    return OCRLine(
        text=text,
        bbox=(0.1, 0.1, 0.5, 0.15),
        polygon=None,
        confidence=0.9,
        line_index=idx,
        page=1,
    )


class FamilyNameExtractorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = {
            "required": True,
            "members": [
                {
                    "canonical_name": "山田 太郎",
                    "aliases": ["山田太郎", "山田太郎様", "ヤマダ タロウ"],
                },
                {
                    "canonical_name": "山田 花子",
                    "aliases": ["山田花子", "ヤマダ ハナコ"],
                },
            ],
        }

    def test_extract_exact_name(self) -> None:
        extractor = FamilyNameExtractor(self.registry)
        candidates = extractor.extract([_line("患者氏名 山田 太郎")])
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].field, FieldName.FAMILY_MEMBER_NAME)
        self.assertEqual(candidates[0].value_normalized, "山田 太郎")
        self.assertEqual(candidates[0].source, "family_registry")

    def test_extract_alias_fuzzy_name(self) -> None:
        extractor = FamilyNameExtractor(self.registry)
        candidates = extractor.extract([_line("ヤマダ タロウー")])
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].value_normalized, "山田 太郎")
        self.assertEqual(candidates[0].source, "family_registry")
        self.assertTrue(any("family_name_alias_fuzzy_match" in r for r in candidates[0].reasons))

    def test_unregistered_same_surname(self) -> None:
        extractor = FamilyNameExtractor(self.registry)
        candidates = extractor.extract([_line("患者氏名 山田 一郎")])
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].source, "family_registry_same_surname")

    def test_unregistered_different_surname(self) -> None:
        extractor = FamilyNameExtractor(self.registry)
        candidates = extractor.extract([_line("患者氏名 佐藤 花子")])
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].source, "family_registry_unknown_surname")

    def test_registry_required(self) -> None:
        with self.assertRaises(FamilyRegistryError):
            FamilyNameExtractor({"required": True, "members": []})


if __name__ == "__main__":
    unittest.main()

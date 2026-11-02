from __future__ import annotations

import unittest

from core.enums import FieldName
from core.models import OCRLine
from app.lambda_handlers.worker_handler import (
    _build_duplicate_key,
    _collect_missing_name_separator_canonicals,
    _detect_non_deductible_keywords,
    _has_family_given_space,
    _is_cancel_last_registration_command,
    _safe_positive_int,
    _is_true,
    _parse_family_registration_entries,
    _parse_s3_uri,
)


class WorkerFamilyRegistrationTest(unittest.TestCase):
    def test_parse_family_registration_entries(self) -> None:
        text = "山田 太郎, ヤマダ タロウ, 山田太朗\n山田 花子"
        entries = _parse_family_registration_entries(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0][0], "山田 太郎")
        self.assertEqual(entries[0][1], ["ヤマダ タロウ", "山田太朗"])
        self.assertEqual(entries[1][0], "山田 花子")
        self.assertEqual(entries[1][1], [])

    def test_parse_family_registration_entries_normalizes_fullwidth_space(self) -> None:
        text = "山田　太郎, ヤマダ　タロウ"
        entries = _parse_family_registration_entries(text)
        self.assertEqual(entries[0][0], "山田 太郎")
        self.assertEqual(entries[0][1], ["ヤマダ タロウ"])

    def test_collect_missing_name_separator_canonicals(self) -> None:
        entries = [("山田太郎", []), ("佐藤 花子", []), ("山田太郎", ["ヤマダ タロウ"])]
        invalid = _collect_missing_name_separator_canonicals(entries)
        self.assertEqual(invalid, ["山田太郎"])

    def test_has_family_given_space(self) -> None:
        self.assertTrue(_has_family_given_space("山田 太郎"))
        self.assertTrue(_has_family_given_space("山田　太郎"))
        self.assertFalse(_has_family_given_space("山田太郎"))

    def test_parse_s3_uri(self) -> None:
        bucket, key = _parse_s3_uri("s3://my-bucket/raw/2026/02/file.jpg")
        self.assertEqual(bucket, "my-bucket")
        self.assertEqual(key, "raw/2026/02/file.jpg")

    def test_build_duplicate_key(self) -> None:
        key = _build_duplicate_key(
            {
                FieldName.PAYMENT_DATE: "2026-02-01",
                FieldName.PAYER_FACILITY_NAME: "テスト医院",
                FieldName.FAMILY_MEMBER_NAME: "山田 太郎",
                FieldName.PAYMENT_AMOUNT: 1000,
            }
        )
        self.assertEqual(key, "2026-02-01|テスト医院|山田太郎|1000")

    def test_detect_non_deductible_keywords_requires_amount(self) -> None:
        line = _line("予防接種 ワクチン", (0.10, 0.10, 0.40, 0.15), 0)
        result = type("Result", (), {"ocr_lines": [line]})()
        found = _detect_non_deductible_keywords(result)
        self.assertEqual(found, [])

    def test_detect_non_deductible_keywords_same_line_amount(self) -> None:
        line = _line("予防接種 3,500円", (0.10, 0.10, 0.40, 0.15), 0)
        result = type("Result", (), {"ocr_lines": [line]})()
        found = _detect_non_deductible_keywords(result)
        self.assertIn("予防接種", found)

    def test_detect_non_deductible_keywords_same_row_amount(self) -> None:
        keyword_line = _line("予防接種", (0.10, 0.20, 0.40, 0.24), 0)
        amount_line = _line("3,500円", (0.70, 0.20, 0.92, 0.24), 1)
        result = type("Result", (), {"ocr_lines": [keyword_line, amount_line]})()
        found = _detect_non_deductible_keywords(result)
        self.assertIn("予防接種", found)

    def test_is_cancel_last_registration_command(self) -> None:
        self.assertTrue(_is_cancel_last_registration_command("取り消し"))
        self.assertTrue(_is_cancel_last_registration_command("  やり直し  "))
        self.assertTrue(_is_cancel_last_registration_command("削除してください"))
        self.assertTrue(_is_cancel_last_registration_command("失敗した"))

    def test_is_cancel_last_registration_command_false(self) -> None:
        self.assertFalse(_is_cancel_last_registration_command(""))
        self.assertFalse(_is_cancel_last_registration_command("今年の医療費"))
        self.assertFalse(_is_cancel_last_registration_command("山田 太郎"))
        self.assertFalse(_is_cancel_last_registration_command("このメッセージは削除できますか"))

    def test_is_true(self) -> None:
        self.assertTrue(_is_true(True))
        self.assertTrue(_is_true("true"))
        self.assertTrue(_is_true("1"))
        self.assertFalse(_is_true(False))
        self.assertFalse(_is_true("false"))
        self.assertFalse(_is_true("0"))

    def test_safe_positive_int(self) -> None:
        self.assertEqual(_safe_positive_int("3", 1), 3)
        self.assertEqual(_safe_positive_int(0, 5), 5)
        self.assertEqual(_safe_positive_int("-1", 5), 5)
        self.assertEqual(_safe_positive_int("abc", 7), 7)


def _line(text: str, bbox: tuple[float, float, float, float], line_index: int) -> OCRLine:
    return OCRLine(
        text=text,
        bbox=bbox,
        polygon=None,
        confidence=0.99,
        line_index=line_index,
        page=1,
    )


if __name__ == "__main__":
    unittest.main()

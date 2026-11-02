from __future__ import annotations

import unittest

from ledger.models import CATEGORY_CARE, CATEGORY_MEDICINE, CATEGORY_TREATMENT, infer_medical_category
from ledger.validation import LedgerValidationError, validate_entry_input


class LedgerValidationTest(unittest.TestCase):
    def test_normalizes_japanese_input(self) -> None:
        result = validate_entry_input(
            {
                "service_date": "２０２６年３月５日",
                "provider_name": "  さくら病院　通院  ",
                "family_member_name": "山田　太郎",
                "amount_yen": "1,200円",
                "reimbursed_yen": "",
            },
            partial=False,
        )
        self.assertEqual(result["service_date"], "2026-03-05")
        self.assertEqual(result["provider_name"], "さくら病院 通院")
        self.assertEqual(result["family_member_name"], "山田 太郎")
        self.assertEqual(result["amount_yen"], 1200)
        self.assertEqual(result["reimbursed_yen"], 0)

    def test_rejects_invalid_values(self) -> None:
        with self.assertRaises(LedgerValidationError) as ctx:
            validate_entry_input(
                {
                    "service_date": "2026-02-30",
                    "amount_yen": "12.5",
                    "medical_category": "交通費",
                    "status": "tentative",
                    "line_user_id": "U2",
                },
                partial=True,
            )
        self.assertEqual(
            set(ctx.exception.errors),
            {"service_date", "amount_yen", "medical_category", "status", "line_user_id"},
        )

    def test_rejects_reimbursed_over_amount(self) -> None:
        with self.assertRaises(LedgerValidationError) as ctx:
            validate_entry_input({"amount_yen": 100, "reimbursed_yen": 101}, partial=True)
        self.assertIn("reimbursed_yen", ctx.exception.errors)

    def test_partial_allows_omitted_required_fields(self) -> None:
        self.assertEqual(validate_entry_input({"note": "  "}, partial=True), {"note": None})

    def test_infer_medical_category(self) -> None:
        self.assertEqual(infer_medical_category("さくら薬局 駅前店"), CATEGORY_MEDICINE)
        self.assertEqual(infer_medical_category("デイサービスあおば"), CATEGORY_CARE)
        self.assertEqual(infer_medical_category("山田内科クリニック"), CATEGORY_TREATMENT)
        self.assertEqual(infer_medical_category(None), CATEGORY_TREATMENT)


if __name__ == "__main__":
    unittest.main()

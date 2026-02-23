from __future__ import annotations

import unittest

from app.pipeline import ReceiptExtractionPipeline
from core.enums import DecisionStatus, FieldName
from core.models import Candidate, Decision


def _candidate(source: str) -> Candidate:
    return Candidate(
        field=FieldName.FAMILY_MEMBER_NAME,
        value_raw="患者氏名 山田 太郎",
        value_normalized="山田 太郎",
        source_line_indices=[1],
        bbox=(0.1, 0.1, 0.4, 0.2),
        score=5.0,
        ocr_confidence=0.9,
        reasons=["test"],
        source=source,
    )


class PipelineFamilyPolicyTest(unittest.TestCase):
    def test_reject_on_different_surname(self) -> None:
        selected = {FieldName.FAMILY_MEMBER_NAME: _candidate("family_registry_unknown_surname")}
        decision = Decision(status=DecisionStatus.AUTO_ACCEPT, confidence=0.8, reasons=["all_required_fields_present"])
        updated = ReceiptExtractionPipeline._apply_family_policy(selected, decision)  # noqa: SLF001
        self.assertEqual(updated.status, DecisionStatus.REJECTED)
        self.assertIn("family_name_not_in_registry_different_surname", updated.reasons)

    def test_review_on_same_surname(self) -> None:
        selected = {FieldName.FAMILY_MEMBER_NAME: _candidate("family_registry_same_surname")}
        decision = Decision(status=DecisionStatus.AUTO_ACCEPT, confidence=0.8, reasons=["all_required_fields_present"])
        updated = ReceiptExtractionPipeline._apply_family_policy(selected, decision)  # noqa: SLF001
        self.assertEqual(updated.status, DecisionStatus.REVIEW_REQUIRED)
        self.assertIn("family_name_not_in_registry_same_surname", updated.reasons)

    def test_keep_status_on_registry_match(self) -> None:
        selected = {FieldName.FAMILY_MEMBER_NAME: _candidate("family_registry")}
        decision = Decision(status=DecisionStatus.AUTO_ACCEPT, confidence=0.8, reasons=["all_required_fields_present"])
        updated = ReceiptExtractionPipeline._apply_family_policy(selected, decision)  # noqa: SLF001
        self.assertEqual(updated.status, DecisionStatus.AUTO_ACCEPT)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import datetime

from core.enums import DecisionStatus, DocumentType, FieldName
from core.models import AuditInfo, Candidate, Decision, ExtractionResult, TemplateMatch
from resolver.year_consistency import apply_year_consistency


def _build_result(name: str, date_value: str, status: DecisionStatus = DecisionStatus.AUTO_ACCEPT) -> ExtractionResult:
    date_candidate = Candidate(
        field=FieldName.PAYMENT_DATE,
        value_raw=date_value,
        value_normalized=date_value,
        source_line_indices=[0],
        bbox=(0.1, 0.1, 0.2, 0.2),
        score=5.0,
        ocr_confidence=0.9,
        reasons=["test"],
        source="generic",
    )
    fields: dict[str, Candidate | None] = {
        FieldName.PAYER_FACILITY_NAME: None,
        FieldName.PRESCRIBING_FACILITY_NAME: None,
        FieldName.PAYMENT_DATE: date_candidate,
        FieldName.PAYMENT_AMOUNT: None,
        FieldName.FAMILY_MEMBER_NAME: None,
    }
    return ExtractionResult(
        document_id=name,
        household_id="household_demo",
        document_type=DocumentType.CLINIC_OR_HOSPITAL,
        template_match=TemplateMatch(matched=False, template_family_id=None, score=0.0),
        fields=fields,
        decision=Decision(status=status, confidence=0.8, reasons=["all_required_fields_present"]),
        audit=AuditInfo(engine="documentai", engine_version="test", pipeline_version="test"),
    )


class YearConsistencyTest(unittest.TestCase):
    def test_out_of_current_or_previous_year_marks_review(self) -> None:
        current_year = datetime.now().year
        previous_year = current_year - 1
        results = [
            _build_result("doc1", f"{current_year}-01-10"),
            _build_result("doc2", f"{previous_year}-02-11"),
            _build_result("doc3", f"{previous_year - 1}-03-12"),
        ]
        config = {"pipeline": {"year_consistency": {"enabled": True}}}
        apply_year_consistency(results, config)
        self.assertEqual(results[0].decision.status, DecisionStatus.AUTO_ACCEPT)
        self.assertEqual(results[1].decision.status, DecisionStatus.AUTO_ACCEPT)
        self.assertEqual(results[2].decision.status, DecisionStatus.REVIEW_REQUIRED)
        self.assertTrue(any("year_out_of_current_or_previous" in r for r in results[2].decision.reasons))

    def test_dominant_year_marks_outlier(self) -> None:
        current_year = datetime.now().year
        previous_year = current_year - 1
        results = [
            _build_result("doc1", f"{current_year}-01-10"),
            _build_result("doc2", f"{current_year}-02-11"),
            _build_result("doc3", f"{current_year}-03-12"),
            _build_result("doc4", f"{previous_year}-03-12"),
            _build_result("doc5", f"{current_year}-04-12"),
        ]
        config = {
            "pipeline": {
                "year_consistency": {
                    "enabled": True,
                    "min_samples": 5,
                    "dominant_ratio_threshold": 0.65,
                    "weight_by_confidence": False,
                }
            }
        }
        apply_year_consistency(results, config)
        self.assertEqual(results[0].decision.status, DecisionStatus.AUTO_ACCEPT)
        self.assertEqual(results[1].decision.status, DecisionStatus.AUTO_ACCEPT)
        self.assertEqual(results[2].decision.status, DecisionStatus.AUTO_ACCEPT)
        self.assertEqual(results[3].decision.status, DecisionStatus.REVIEW_REQUIRED)
        self.assertEqual(results[4].decision.status, DecisionStatus.AUTO_ACCEPT)
        self.assertTrue(any("year_outlier_against_batch" in r for r in results[3].decision.reasons))

    def test_ratio_not_enough_then_no_change(self) -> None:
        current_year = datetime.now().year
        previous_year = current_year - 1
        results = [
            _build_result("doc1", f"{current_year}-01-10"),
            _build_result("doc2", f"{current_year}-02-11"),
            _build_result("doc3", f"{previous_year}-03-12"),
            _build_result("doc4", f"{previous_year}-04-13"),
            _build_result("doc5", f"{current_year}-05-14"),
        ]
        config = {
            "pipeline": {
                "year_consistency": {
                    "enabled": True,
                    "min_samples": 5,
                    "dominant_ratio_threshold": 0.8,
                    "weight_by_confidence": False,
                }
            }
        }
        apply_year_consistency(results, config)
        self.assertTrue(all(r.decision.status == DecisionStatus.AUTO_ACCEPT for r in results))

    def test_rejected_status_is_not_downgraded(self) -> None:
        current_year = datetime.now().year
        previous_year = current_year - 1
        kept_rejected = _build_result("doc_rej", f"{previous_year}-01-01", status=DecisionStatus.REJECTED)
        normal = _build_result("doc_ok", f"{current_year}-01-01")
        normal2 = _build_result("doc_ok2", f"{current_year}-02-01")
        normal3 = _build_result("doc_ok3", f"{current_year}-03-01")
        normal4 = _build_result("doc_ok4", f"{current_year}-04-01")
        results = [kept_rejected, normal, normal2, normal3, normal4]
        config = {
            "pipeline": {
                "year_consistency": {
                    "enabled": True,
                    "min_samples": 5,
                    "dominant_ratio_threshold": 0.65,
                    "weight_by_confidence": False,
                }
            }
        }
        apply_year_consistency(results, config)
        self.assertEqual(kept_rejected.decision.status, DecisionStatus.REJECTED)
        self.assertTrue(any("year_outlier_against_batch" in r for r in kept_rejected.decision.reasons))


if __name__ == "__main__":
    unittest.main()

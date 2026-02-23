from __future__ import annotations

from typing import Any

from core.enums import DecisionStatus, FieldName
from core.models import Candidate, Decision, TemplateMatch
from resolver.confidence import overall_confidence


class DecisionResolver:
    def __init__(
        self,
        review_threshold: float = 0.72,
        reject_threshold: float = 0.35,
        candidate_threshold: float = 2.5,
    ) -> None:
        self.review_threshold = review_threshold
        self.reject_threshold = reject_threshold
        self.candidate_threshold = candidate_threshold

    def resolve(
        self,
        candidate_pool: dict[str, list[Candidate]],
        template_match: TemplateMatch,
        ocr_quality: float,
    ) -> tuple[dict[str, Candidate | None], Decision]:
        selected = self._select_fields(candidate_pool)

        missing_required = [
            field_name
            for field_name in FieldName.REQUIRED_FIELDS
            if selected.get(field_name) is None
        ]

        conf = overall_confidence(
            selected_fields=selected,
            template_score=template_match.score if template_match.matched else 0.0,
            ocr_quality=ocr_quality,
        )
        reasons: list[str] = []
        status = DecisionStatus.AUTO_ACCEPT

        any_candidates = any(candidate_pool.get(key) for key in candidate_pool.keys())
        if not any_candidates or ocr_quality < 0.25:
            status = DecisionStatus.REJECTED
            reasons.extend(["no_viable_candidates", "low_ocr_quality"])
        elif conf < self.reject_threshold:
            status = DecisionStatus.REJECTED
            reasons.append("overall_confidence_below_reject_threshold")
        elif missing_required:
            status = DecisionStatus.REVIEW_REQUIRED
            reasons.append(f"missing_required_fields:{','.join(missing_required)}")
        elif conf < self.review_threshold:
            status = DecisionStatus.REVIEW_REQUIRED
            reasons.append("overall_confidence_below_review_threshold")
        else:
            reasons.append("all_required_fields_present")

        if template_match.matched and template_match.score >= 0.8:
            reasons.append("template_match_strong")
        elif template_match.matched:
            reasons.append("template_match_applied")

        decision = Decision(status=status, confidence=conf, reasons=reasons)
        return selected, decision

    def _select_fields(self, pool: dict[str, list[Candidate]]) -> dict[str, Candidate | None]:
        selected: dict[str, Candidate | None] = {
            FieldName.PAYER_FACILITY_NAME: None,
            FieldName.PRESCRIBING_FACILITY_NAME: None,
            FieldName.PAYMENT_DATE: None,
            FieldName.PAYMENT_AMOUNT: None,
            FieldName.FAMILY_MEMBER_NAME: None,
        }

        for field_name, candidates in pool.items():
            if not candidates:
                continue
            best = sorted(candidates, key=lambda c: (c.score, c.ocr_confidence), reverse=True)[0]
            threshold = self.candidate_threshold
            if best.source == "template":
                threshold -= 0.7
            if best.score >= threshold:
                selected[field_name] = best
        return selected


def resolver_from_config(config: dict[str, Any]) -> DecisionResolver:
    pipeline = config.get("pipeline", {})
    return DecisionResolver(
        review_threshold=float(pipeline.get("review_threshold", 0.72)),
        reject_threshold=float(pipeline.get("reject_threshold", 0.35)),
        candidate_threshold=float(pipeline.get("candidate_threshold", 2.5)),
    )

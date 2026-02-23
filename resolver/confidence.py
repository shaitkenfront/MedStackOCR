from __future__ import annotations

from core.models import Candidate
from extractors.common import clamp01, score_to_unit


def candidate_confidence(candidate: Candidate) -> float:
    score_part = score_to_unit(candidate.score, low=0.0, high=10.0)
    return clamp01(0.55 * candidate.ocr_confidence + 0.45 * score_part)


def overall_confidence(
    selected_fields: dict[str, Candidate | None],
    template_score: float,
    ocr_quality: float,
) -> float:
    field_scores = [candidate_confidence(c) for c in selected_fields.values() if c is not None]
    base = sum(field_scores) / len(field_scores) if field_scores else 0.0
    if template_score <= 0:
        return clamp01(0.80 * base + 0.20 * ocr_quality)
    return clamp01(0.65 * base + 0.20 * template_score + 0.15 * ocr_quality)

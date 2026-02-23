from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from core.enums import DecisionStatus, FieldName
from core.models import Candidate, Decision, ExtractionResult


def apply_year_consistency(results: list[ExtractionResult], config: dict[str, Any]) -> None:
    if not results:
        return

    policy = _load_policy(config)
    if not policy["enabled"]:
        return

    target_tax_year = policy["target_tax_year"]
    if target_tax_year is not None:
        _apply_with_target_year(results, target_tax_year)
        return

    _apply_with_dominant_year(results, policy)


def _apply_with_target_year(results: list[ExtractionResult], target_year: int) -> None:
    for result in results:
        year, _ = _extract_payment_year_and_weight(result, weight_by_confidence=True)
        if year is None or year == target_year:
            continue
        reason = f"year_mismatch_target_tax_year:target={target_year}:doc={year}"
        _force_review_required(result, reason)


def _apply_with_dominant_year(results: list[ExtractionResult], policy: dict[str, Any]) -> None:
    weight_by_confidence = bool(policy["weight_by_confidence"])
    min_samples = int(policy["min_samples"])
    ratio_threshold = float(policy["dominant_ratio_threshold"])

    year_weights: dict[int, float] = defaultdict(float)
    valid_count = 0
    for result in results:
        year, weight = _extract_payment_year_and_weight(result, weight_by_confidence=weight_by_confidence)
        if year is None:
            continue
        valid_count += 1
        year_weights[year] += weight

    if valid_count < min_samples:
        return
    total_weight = sum(year_weights.values())
    if total_weight <= 0:
        return

    dominant_year, dominant_weight = max(year_weights.items(), key=lambda item: item[1])
    dominant_ratio = dominant_weight / total_weight
    if dominant_ratio < ratio_threshold:
        return

    for result in results:
        year, _ = _extract_payment_year_and_weight(result, weight_by_confidence=weight_by_confidence)
        if year is None or year == dominant_year:
            continue
        reason = (
            f"year_outlier_against_batch:dominant={dominant_year}:doc={year}:"
            f"ratio={dominant_ratio:.3f}"
        )
        _force_review_required(result, reason)


def _extract_payment_year_and_weight(
    result: ExtractionResult,
    *,
    weight_by_confidence: bool,
) -> tuple[int | None, float]:
    candidate = result.fields.get(FieldName.PAYMENT_DATE)
    if not isinstance(candidate, Candidate):
        return None, 0.0

    value = candidate.value_normalized
    if value is None:
        return None, 0.0
    text = str(value).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None, 0.0

    if weight_by_confidence:
        weight = max(0.0, min(1.0, float(candidate.ocr_confidence)))
        return parsed.year, weight
    return parsed.year, 1.0


def _force_review_required(result: ExtractionResult, reason: str) -> None:
    reasons = list(result.decision.reasons)
    if reason not in reasons:
        reasons.append(reason)

    status = result.decision.status
    if status != DecisionStatus.REJECTED:
        status = DecisionStatus.REVIEW_REQUIRED

    result.decision = Decision(status=status, confidence=result.decision.confidence, reasons=reasons)
    if reason not in result.audit.notes:
        result.audit.notes.append(reason)


def _load_policy(config: dict[str, Any]) -> dict[str, Any]:
    pipeline = config.get("pipeline", {})
    year_conf = pipeline.get("year_consistency", {})
    if not isinstance(year_conf, dict):
        year_conf = {}

    target_tax_year = _parse_optional_int(pipeline.get("target_tax_year"))
    return {
        "enabled": bool(year_conf.get("enabled", True)),
        "min_samples": int(year_conf.get("min_samples", 5)),
        "dominant_ratio_threshold": float(year_conf.get("dominant_ratio_threshold", 0.65)),
        "weight_by_confidence": bool(year_conf.get("weight_by_confidence", True)),
        "target_tax_year": target_tax_year,
    }


def _parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None

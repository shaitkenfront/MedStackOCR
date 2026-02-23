from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.enums import FieldName


def _normalize_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            datetime.strptime(text, "%Y-%m-%d")
            return text
        dt = datetime.fromisoformat(text)
        return dt.date().isoformat()
    except Exception:
        return text


def _normalize_amount(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace(",", "").strip()
    if text.endswith("円"):
        text = text[:-1].strip()
    if text.startswith(("¥", "￥")):
        text = text[1:].strip()
    if not text.isdigit():
        return None
    return int(text)


def extract_result_value(result: dict[str, Any], field_name: str) -> Any:
    fields = result.get("fields", {})
    if not isinstance(fields, dict):
        return None
    candidate = fields.get(field_name)
    if not isinstance(candidate, dict):
        return None
    return candidate.get("value_normalized")


@dataclass
class FieldMetric:
    total: int = 0
    correct: int = 0

    @property
    def accuracy(self) -> float:
        if self.total == 0:
            return 0.0
        return self.correct / self.total


@dataclass
class EvalMetrics:
    by_field: dict[str, FieldMetric] = field(
        default_factory=lambda: {
            FieldName.PAYER_FACILITY_NAME: FieldMetric(),
            FieldName.PAYMENT_DATE: FieldMetric(),
            FieldName.PAYMENT_AMOUNT: FieldMetric(),
        }
    )
    status_counts: dict[str, int] = field(default_factory=dict)
    total_documents: int = 0

    def add(self, predicted: dict[str, Any], ground_truth: dict[str, Any]) -> None:
        self.total_documents += 1

        status = predicted.get("decision", {}).get("status")
        if isinstance(status, str):
            self.status_counts[status] = self.status_counts.get(status, 0) + 1

        truth_fields = ground_truth.get("fields", {})
        if not isinstance(truth_fields, dict):
            return

        for field_name, metric in self.by_field.items():
            if field_name not in truth_fields:
                continue
            metric.total += 1
            pred_value = extract_result_value(predicted, field_name)
            gt_value = truth_fields.get(field_name)
            if field_name == FieldName.PAYMENT_DATE:
                if _normalize_date(pred_value) == _normalize_date(gt_value):
                    metric.correct += 1
            elif field_name == FieldName.PAYMENT_AMOUNT:
                if _normalize_amount(pred_value) == _normalize_amount(gt_value):
                    metric.correct += 1
            else:
                if str(pred_value or "").strip() == str(gt_value or "").strip():
                    metric.correct += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_documents": self.total_documents,
            "by_field": {
                key: {
                    "correct": metric.correct,
                    "total": metric.total,
                    "accuracy": round(metric.accuracy, 4),
                }
                for key, metric in self.by_field.items()
            },
            "status_counts": self.status_counts,
        }


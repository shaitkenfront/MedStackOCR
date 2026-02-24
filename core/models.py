from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from core.enums import DecisionStatus, DocumentType

BBox = tuple[float, float, float, float]
Polygon = list[tuple[float, float]]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {k: _serialize(v) for k, v in asdict(value).items()}
    if isinstance(value, tuple):
        return [_serialize(v) for v in value]
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


@dataclass(slots=True)
class OCRRawResult:
    engine: str
    engine_version: str
    payload: Any
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OCRLine:
    text: str
    bbox: BBox
    polygon: Optional[Polygon]
    confidence: float
    line_index: int
    page: int = 1
    raw: dict[str, Any] = field(default_factory=dict)

    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2, (y1 + y2) / 2)


@dataclass(slots=True)
class Candidate:
    field: str
    value_raw: Any
    value_normalized: Any
    source_line_indices: list[int]
    bbox: Optional[BBox]
    score: float
    ocr_confidence: float
    reasons: list[str]
    source: str = "generic"


@dataclass(slots=True)
class TemplateMatch:
    matched: bool
    template_family_id: Optional[str]
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Decision:
    status: DecisionStatus
    confidence: float
    reasons: list[str]


@dataclass(slots=True)
class AuditInfo:
    engine: str
    engine_version: str
    pipeline_version: str
    processed_at: str = field(default_factory=utc_now_iso)
    classifier_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExtractionResult:
    document_id: str
    household_id: str | None
    document_type: DocumentType
    template_match: TemplateMatch
    fields: dict[str, Optional[Candidate]]
    decision: Decision
    audit: AuditInfo
    candidate_pool: dict[str, list[Candidate]] = field(default_factory=dict)
    ocr_lines: list[OCRLine] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)

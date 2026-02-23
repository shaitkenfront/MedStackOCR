from __future__ import annotations

from typing import Any

from core.models import AuditInfo

PIPELINE_VERSION = "0.1.0"


class AuditLogger:
    def create(
        self,
        engine: str,
        engine_version: str,
        classifier_reasons: list[str] | None = None,
        notes: list[str] | None = None,
    ) -> AuditInfo:
        return AuditInfo(
            engine=engine,
            engine_version=engine_version,
            pipeline_version=PIPELINE_VERSION,
            classifier_reasons=classifier_reasons or [],
            notes=notes or [],
        )

    @staticmethod
    def append_note(audit: AuditInfo, note: str) -> None:
        audit.notes.append(note)


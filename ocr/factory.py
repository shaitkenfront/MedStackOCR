from __future__ import annotations

from typing import Any

from ocr.base import OCRAdapter, OCRAdapterError
from ocr.documentai_adapter import GoogleDocumentAIAdapter
from ocr.mock_adapter import MockOCRAdapter


def _canonical_engine_name(name: str) -> str:
    lowered = name.strip().lower()
    if lowered in {"document_ai", "google-documentai", "google_documentai"}:
        return "documentai"
    return lowered


def _resolve_allowed_engines(config: dict[str, Any]) -> set[str]:
    ocr_conf = config.get("ocr", {})
    allowed = ocr_conf.get("allowed_engines")
    if isinstance(allowed, list):
        resolved = {_canonical_engine_name(str(item)) for item in allowed if str(item).strip()}
        if resolved:
            return resolved

    configured = _canonical_engine_name(str(ocr_conf.get("engine", "documentai")))
    return {configured}


def _assert_engine_available(name: str, ocr_config: dict[str, Any]) -> None:
    conf = ocr_config.get(name)
    if isinstance(conf, dict) and not bool(conf.get("enabled", False)):
        raise OCRAdapterError(f"OCR engine is disabled in config: {name}")


def create_ocr_adapter(engine_name: str, config: dict[str, Any]) -> OCRAdapter:
    configured = str(config.get("ocr", {}).get("engine", "documentai"))
    requested = engine_name or configured
    name = _canonical_engine_name(requested)
    ocr_config = config.get("ocr", {}).get("engines", {})
    allowed_engines = _resolve_allowed_engines(config)

    if name not in allowed_engines:
        allowed = ",".join(sorted(allowed_engines))
        raise OCRAdapterError(f"OCR engine is locked. requested={name} allowed={allowed}")

    if name == "mock":
        _assert_engine_available(name, ocr_config)
        fixture_dir = config.get("mock_fixture_dir")
        return MockOCRAdapter(fixture_dir=fixture_dir)

    if name == "documentai":
        _assert_engine_available(name, ocr_config)
        doc_conf = ocr_config.get("documentai", {})
        return GoogleDocumentAIAdapter(
            project_id=doc_conf.get("project_id"),
            location=doc_conf.get("location", "us"),
            processor_id=doc_conf.get("processor_id"),
            processor_version=doc_conf.get("processor_version"),
            endpoint=doc_conf.get("endpoint"),
            credentials_path=doc_conf.get("credentials_path"),
            timeout_sec=int(doc_conf.get("timeout_sec", 120)),
            mime_type=doc_conf.get("mime_type"),
            field_mask=doc_conf.get("field_mask"),
        )

    raise OCRAdapterError(f"unsupported OCR engine: {engine_name}")

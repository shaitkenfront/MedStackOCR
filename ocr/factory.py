from __future__ import annotations

import os
import shlex
from typing import Any

from ocr.base import OCRAdapter, OCRAdapterError
from ocr.deepseek_adapter import DeepSeekOCRAdapter
from ocr.documentai_adapter import GoogleDocumentAIAdapter
from ocr.mock_adapter import MockOCRAdapter
from ocr.ndlocr_lite_adapter import NdlOcrLiteAdapter
from ocr.paddle_adapter import PaddleOCRAdapter
from ocr.tesseract_adapter import TesseractAdapter
from ocr.yomitoku_adapter import YomitokuOCRAdapter


def _to_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [item for item in shlex.split(text, posix=os.name != "nt") if item]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def create_ocr_adapter(engine_name: str, config: dict[str, Any]) -> OCRAdapter:
    name = (engine_name or "mock").strip().lower()
    ocr_config = config.get("ocr", {}).get("engines", {})

    if name == "mock":
        fixture_dir = config.get("mock_fixture_dir")
        return MockOCRAdapter(fixture_dir=fixture_dir)

    if name == "tesseract":
        tconf = ocr_config.get("tesseract", {})
        lang = tconf.get("lang", "jpn")
        tesseract_cmd = tconf.get("cmd")
        tessdata_dir = tconf.get("tessdata_dir")
        return TesseractAdapter(
            lang=lang,
            tesseract_cmd=tesseract_cmd,
            tessdata_dir=tessdata_dir,
        )

    if name == "paddle":
        pconf = ocr_config.get("paddle", {})
        lang = pconf.get("lang", "ja")
        use_gpu = bool(pconf.get("use_gpu", True))
        ocr_version = pconf.get("ocr_version")
        return PaddleOCRAdapter(lang=lang, use_gpu=use_gpu, ocr_version=ocr_version)

    if name == "yomitoku":
        yconf = ocr_config.get("yomitoku", {})
        device = yconf.get("device", "cuda")
        visualize = bool(yconf.get("visualize", False))
        return YomitokuOCRAdapter(device=device, visualize=visualize)

    if name in {"deepseek", "deepseek-ocr", "deepseek_ocr"}:
        dconf = ocr_config.get("deepseek", {})
        return DeepSeekOCRAdapter(
            api_key_env=dconf.get("api_key_env", "DS_OCR_API_KEY"),
            api_key=dconf.get("api_key"),
            base_url=dconf.get("base_url"),
            model_name=dconf.get("model_name"),
            backend=dconf.get("backend", "api"),
            mode=dconf.get("mode", "free_ocr"),
            dpi=int(dconf.get("dpi", 200)),
            local_prompt=dconf.get("local_prompt"),
            local_output_dir=dconf.get("local_output_dir"),
            local_base_size=int(dconf.get("local_base_size", 512)),
            local_image_size=int(dconf.get("local_image_size", 512)),
            local_crop_mode=bool(dconf.get("local_crop_mode", False)),
            local_device=dconf.get("local_device", "cuda"),
            local_dtype=dconf.get("local_dtype", "bfloat16"),
            local_attn_impl=dconf.get("local_attn_impl", "eager"),
            local_trust_remote_code=bool(dconf.get("local_trust_remote_code", True)),
        )

    if name in {"ndlocr-lite", "ndlocr_lite", "ndllite"}:
        nconf = ocr_config.get("ndlocr_lite", {})
        return NdlOcrLiteAdapter(
            command=nconf.get("command", "python src/ocr.py"),
            working_dir=nconf.get("working_dir"),
            device=str(nconf.get("device", "cpu")),
            viz=bool(nconf.get("viz", False)),
            timeout_sec=int(nconf.get("timeout_sec", 600)),
            extra_args=_to_str_list(nconf.get("extra_args")),
        )

    if name in {"documentai", "document_ai", "google-documentai"}:
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

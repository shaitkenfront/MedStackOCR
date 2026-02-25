from __future__ import annotations

from typing import Any

from ocr.base import OCRAdapter, OCRAdapterError
from ocr.deepseek_adapter import DeepSeekOCRAdapter
from ocr.mock_adapter import MockOCRAdapter
from ocr.paddle_adapter import PaddleOCRAdapter
from ocr.tesseract_adapter import TesseractAdapter
from ocr.yomitoku_adapter import YomitokuOCRAdapter


def _canonical_engine_name(name: str) -> str:
    lowered = name.strip().lower()
    if lowered in {"deepseek-ocr", "deepseek_ocr"}:
        return "deepseek"
    return lowered


def _resolve_allowed_engines(config: dict[str, Any]) -> set[str]:
    ocr_conf = config.get("ocr", {})
    allowed = ocr_conf.get("allowed_engines")
    if isinstance(allowed, list):
        resolved = {_canonical_engine_name(str(item)) for item in allowed if str(item).strip()}
        if resolved:
            return resolved

    configured = _canonical_engine_name(str(ocr_conf.get("engine", "yomitoku")))
    return {configured}


def _assert_engine_available(name: str, ocr_config: dict[str, Any]) -> None:
    conf = ocr_config.get(name)
    if isinstance(conf, dict) and not bool(conf.get("enabled", False)):
        raise OCRAdapterError(f"OCR engine is disabled in config: {name}")


def create_ocr_adapter(engine_name: str, config: dict[str, Any]) -> OCRAdapter:
    configured = str(config.get("ocr", {}).get("engine", "yomitoku"))
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

    if name == "tesseract":
        _assert_engine_available(name, ocr_config)
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
        _assert_engine_available(name, ocr_config)
        pconf = ocr_config.get("paddle", {})
        lang = pconf.get("lang", "ja")
        use_gpu = bool(pconf.get("use_gpu", True))
        ocr_version = pconf.get("ocr_version")
        return PaddleOCRAdapter(lang=lang, use_gpu=use_gpu, ocr_version=ocr_version)

    if name == "yomitoku":
        _assert_engine_available(name, ocr_config)
        yconf = ocr_config.get("yomitoku", {})
        device = yconf.get("device", "cuda")
        visualize = bool(yconf.get("visualize", False))
        return YomitokuOCRAdapter(device=device, visualize=visualize)

    if name == "deepseek":
        _assert_engine_available(name, ocr_config)
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

    raise OCRAdapterError(f"unsupported OCR engine: {engine_name}")

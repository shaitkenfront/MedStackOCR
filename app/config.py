from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "pipeline": {
        "review_threshold": 0.72,
        "reject_threshold": 0.35,
        "candidate_threshold": 2.5,
        "target_tax_year": None,
        "year_consistency": {
            "enabled": True,
            "min_samples": 5,
            "dominant_ratio_threshold": 0.65,
            "weight_by_confidence": True,
        },
    },
    "ocr": {
        "engine": "yomitoku",
        "allowed_engines": ["yomitoku"],
        "engines": {
            "mock": {"enabled": False},
            "tesseract": {
                "enabled": False,
                "lang": "jpn+eng",
                "cmd": r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                "tessdata_dir": "data/tessdata",
            },
            "paddle": {
                "enabled": False,
                "lang": "ja",
                "use_gpu": True,
                "ocr_version": None,
            },
            "yomitoku": {
                "enabled": True,
                "device": "cuda",
                "visualize": False,
            },
            "deepseek": {
                "enabled": False,
                "backend": "api",
                "api_key_env": "DS_OCR_API_KEY",
                "api_key": None,
                "base_url": None,
                "model_name": None,
                "mode": "free_ocr",
                "dpi": 200,
                "local_prompt": None,
                "local_output_dir": None,
                "local_base_size": 512,
                "local_image_size": 512,
                "local_crop_mode": False,
                "local_device": "cuda",
                "local_dtype": "bfloat16",
                "local_attn_impl": "eager",
                "local_trust_remote_code": True,
            },
            "vision": {"enabled": False},
            "documentai": {"enabled": False},
        },
    },
    "templates": {
        "store_path": "data/templates",
        "household_match_threshold": 0.65,
    },
    "family_registry": {
        "required": True,
        "members": [
            {
                "canonical_name": "山田 太郎",
                "aliases": ["山田太郎", "山田 太郎様", "ヤマダ タロウ", "ヤマタ タロウ"],
            },
            {
                "canonical_name": "山田 花子",
                "aliases": ["山田花子", "山田 花子様", "ヤマダ ハナコ", "ヤマタ ハナコ"],
            },
        ],
    },
    "output": {
        "save_audit": True,
        "pretty_json": True,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | None = None) -> dict[str, Any]:
    if not config_path:
        return DEFAULT_CONFIG

    path = Path(config_path)
    if not path.exists():
        return DEFAULT_CONFIG

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return DEFAULT_CONFIG

    data: dict[str, Any] | None = None
    if path.suffix.lower() == ".json":
        import json

        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except Exception:
            data = {}
        else:
            loaded = yaml.safe_load(text)
            data = loaded if isinstance(loaded, dict) else {}

    if data is None:
        data = {}
    return deep_merge(DEFAULT_CONFIG, data)

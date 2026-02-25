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
        "engine": "documentai",
        "allowed_engines": ["documentai"],
        "engines": {
            "mock": {"enabled": False},
            "documentai": {
                "enabled": True,
                "project_id": None,
                "location": "us",
                "processor_id": None,
                "processor_version": None,
                "endpoint": None,
                "credentials_path": None,
                "timeout_sec": 120,
                "mime_type": None,
                "field_mask": None,
                "amount_tuning": {
                    "exclude_context_currency_primary_penalty": 1.0,
                    "currency_primary_bonus": 1.2,
                    "near_secondary_without_currency_penalty": 1.8,
                    "address_context_penalty": 2.2,
                    "medication_context_penalty": 1.8,
                    "long_text_number_penalty": 2.2,
                    "long_text_min_length": 28,
                    "long_text_min_digits": 3,
                    "small_plain_number_penalty": 2.4,
                    "label_alignment_bonus_max": 3.0,
                    "label_alignment_max_dx": 0.25,
                    "label_alignment_max_dy": 0.08,
                    "label_anchor_max_length": 16,
                    "label_anchor_max_digits": 2,
                },
            },
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
    "notifications": {
        "enabled": False,
        "channels": [],
        "max_items_in_message": 10,
        "line": {
            "channel_access_token": None,
            "to": None,
        },
        "slack": {
            "webhook_url": None,
        },
        "discord": {
            "webhook_url": None,
        },
    },
    "line_messaging": {
        "enabled": False,
        "channel_secret": None,
        "channel_access_token": None,
        "webhook_path": "/webhook/line",
        "api_base_url": "https://api.line.me",
        "data_api_base_url": "https://api-data.line.me",
        "timeout_sec": 10,
        "allowed_user_ids": [],
        "default_household_id": None,
    },
    "inbox": {
        "backend": "sqlite",
        "sqlite_path": "data/inbox/linebot.db",
        "dynamodb": {
            "region": None,
            "table_prefix": "medstackocr",
            "event_ttl_days": 7,
            "tables": {
                "event_dedupe": None,
                "receipts": None,
                "receipt_fields": None,
                "sessions": None,
                "aggregate_entries": None,
                "family_registry": None,
            },
        },
        "image_store_dir": "data/inbox/images",
        "image_retention_days": 14,
        "session_ttl_minutes": 60,
        "max_candidate_options": 3,
        "enable_text_commands": True,
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

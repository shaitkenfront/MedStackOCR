from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from core.enums import FieldName
from io_utils.json_writer import load_json, write_json

REGISTRY_VERSION = 1


def load_processed_registry(path: str | Path) -> dict[str, dict[str, int]]:
    registry_path = Path(path)
    if not registry_path.exists():
        return {}

    try:
        payload = load_json(registry_path)
    except Exception:
        return {}

    items = payload.get("items")
    if not isinstance(items, dict):
        return {}

    registry: dict[str, dict[str, int]] = {}
    for key, value in items.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        size = _safe_int(value.get("size"))
        mtime_ns = _safe_int(value.get("mtime_ns"))
        if size is None or mtime_ns is None:
            continue
        registry[key] = {"size": size, "mtime_ns": mtime_ns}
    return registry


def save_processed_registry(path: str | Path, registry: dict[str, dict[str, int]]) -> Path:
    payload = {
        "version": REGISTRY_VERSION,
        "items": registry,
    }
    return write_json(path, payload, pretty=True)


def is_already_processed(registry: dict[str, dict[str, int]], image_path: Path) -> bool:
    signature = _build_signature(image_path)
    cached = registry.get(signature["path"])
    if not isinstance(cached, dict):
        return False
    return int(cached.get("size", -1)) == signature["size"] and int(cached.get("mtime_ns", -1)) == signature["mtime_ns"]


def update_processed_registry(registry: dict[str, dict[str, int]], image_path: Path) -> None:
    signature = _build_signature(image_path)
    registry[signature["path"]] = {
        "size": signature["size"],
        "mtime_ns": signature["mtime_ns"],
    }


def write_summary_csv(output_dir: str | Path) -> Path:
    base = Path(output_dir)
    csv_path = base / "summary.csv"
    rows: list[list[str]] = []

    for result_path in sorted(base.glob("*.result.json")):
        try:
            result = load_json(result_path)
        except Exception:
            continue
        fields = result.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}

        date_text = _field_value(fields, FieldName.PAYMENT_DATE)
        family_name = _field_value(fields, FieldName.FAMILY_MEMBER_NAME)
        facility = _field_value(fields, FieldName.PAYER_FACILITY_NAME)
        if not facility:
            facility = _field_value(fields, FieldName.PRESCRIBING_FACILITY_NAME)
        amount = _field_value(fields, FieldName.PAYMENT_AMOUNT)
        rows.append([date_text, family_name, facility, amount])

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["日付", "氏名", "医療機関・調剤薬局名", "金額"])
        writer.writerows(rows)

    return csv_path


def _build_signature(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    stat = resolved.stat()
    mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
    return {
        "path": str(resolved),
        "size": int(stat.st_size),
        "mtime_ns": int(mtime_ns),
    }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _field_value(fields: dict[str, Any], field_name: str) -> str:
    candidate = fields.get(field_name)
    if not isinstance(candidate, dict):
        return ""
    value = candidate.get("value_normalized")
    if value in (None, ""):
        value = candidate.get("value_raw")
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable

from core.enums import FieldName
from io_utils.json_writer import load_json
from notifications.factory import build_notification_channels


@dataclass(slots=True)
class NotificationResult:
    sent_channels: list[str]
    failed_channels: dict[str, str]
    message: str | None = None
    skipped: bool = False


class NotificationService:
    def __init__(
        self,
        config: dict[str, Any],
        channel_builder: Callable[
            [dict[str, Any]],
            tuple[dict[str, Any], dict[str, str]],
        ] = build_notification_channels,
    ) -> None:
        nconf = config.get("notifications", {})
        self.enabled = bool(nconf.get("enabled", False))
        self.max_items = _safe_int(nconf.get("max_items_in_message", 10), default=10, minimum=1)
        self._channels, self._build_errors = channel_builder(config)

    def notify_new_receipts(self, target_dir: Path, new_images: list[Path]) -> NotificationResult:
        if not self.enabled or not new_images:
            return NotificationResult(sent_channels=[], failed_channels={}, skipped=True)

        message = self._build_new_receipts_message(target_dir, new_images)
        sent: list[str] = []
        failed = dict(self._build_errors)

        for name, notifier in self._channels.items():
            try:
                notifier.send(message)
                sent.append(name)
            except Exception as exc:  # noqa: BLE001
                failed[name] = str(exc)

        return NotificationResult(sent_channels=sent, failed_channels=failed, message=message, skipped=False)

    def _build_new_receipts_message(self, target_dir: Path, new_images: list[Path]) -> str:
        sorted_names = sorted(path.name for path in new_images)
        details = [self._load_receipt_detail(target_dir, path) for path in sorted(new_images, key=lambda p: p.name)]
        preview = details[: self.max_items]
        remainder = max(0, len(details) - len(preview))
        total_amount = self._sum_current_total_amount(target_dir)

        lines = [
            "[MedStackOCR] 新しい領収書を検知しました",
            f"現時点での医療費合計: {total_amount}",
            f"件数: {len(sorted_names)}",
        ]
        for item in preview:
            lines.append(
                f"- {item['date']}, {item['patient_name']}, "
                f"{item['clinic_or_pharmacy_name']}, {item['amount']}"
            )
        if remainder > 0:
            lines.append(f"- ... 他 {remainder} 件")
        return "\n".join(lines)

    def _load_receipt_detail(self, target_dir: Path, image_path: Path) -> dict[str, str]:
        result_path = target_dir / f"{image_path.stem}.result.json"
        if not result_path.exists():
            return {
                "date": "",
                "patient_name": "",
                "clinic_or_pharmacy_name": image_path.name,
                "amount": "",
            }
        try:
            payload = load_json(result_path)
        except Exception:
            return {
                "date": "",
                "patient_name": "",
                "clinic_or_pharmacy_name": image_path.name,
                "amount": "",
            }

        fields = payload.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}

        date = self._field_text(fields, FieldName.PAYMENT_DATE)
        patient_name = self._field_text(fields, FieldName.FAMILY_MEMBER_NAME)
        clinic = self._field_text(fields, FieldName.PAYER_FACILITY_NAME)
        if not clinic:
            clinic = self._field_text(fields, FieldName.PRESCRIBING_FACILITY_NAME)
        amount = self._field_text(fields, FieldName.PAYMENT_AMOUNT)
        return {
            "date": date,
            "patient_name": patient_name,
            "clinic_or_pharmacy_name": clinic,
            "amount": amount,
        }

    def _sum_current_total_amount(self, target_dir: Path) -> int:
        total = 0
        for result_path in sorted(target_dir.glob("*.result.json")):
            try:
                payload = load_json(result_path)
            except Exception:
                continue
            fields = payload.get("fields", {})
            if not isinstance(fields, dict):
                continue
            amount_candidate = fields.get(FieldName.PAYMENT_AMOUNT)
            amount = self._to_int_amount(amount_candidate)
            if amount is None:
                continue
            total += amount
        return total

    @staticmethod
    def _field_text(fields: dict[str, Any], field_name: str) -> str:
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

    @staticmethod
    def _to_int_amount(candidate: Any) -> int | None:
        if not isinstance(candidate, dict):
            return None

        for key in ("value_normalized", "value_raw"):
            value = candidate.get(key)
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value) if value.is_integer() else None
            text = str(value).strip()
            if not text:
                continue
            digits = re.sub(r"[^\d\-]", "", text)
            if not digits or digits in {"-", "--"}:
                continue
            try:
                return int(digits)
            except ValueError:
                continue
        return None


def _safe_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        resolved = int(value)
    except Exception:
        return default
    return max(minimum, resolved)

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ledger.models import (
    MEDICAL_CATEGORIES,
    STATUS_CONFIRMED,
    STATUS_HOLD,
    to_int,
)

EDITABLE_FIELDS: tuple[str, ...] = (
    "service_date",
    "provider_name",
    "family_member_name",
    "amount_yen",
    "reimbursed_yen",
    "medical_category",
    "note",
    "status",
)
REQUIRED_FOR_CREATE: tuple[str, ...] = (
    "service_date",
    "provider_name",
    "family_member_name",
    "amount_yen",
)
# 利用者が明細画面から設定できる状態。tentative は OCR 直後の内部状態なので選ばせない。
USER_SETTABLE_STATUSES: tuple[str, ...] = (STATUS_CONFIRMED, STATUS_HOLD)

MAX_AMOUNT_YEN = 100_000_000
MAX_PROVIDER_LENGTH = 100
MAX_FAMILY_NAME_LENGTH = 50
MAX_NOTE_LENGTH = 500

_DATE_RE = re.compile(r"^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*$")


class LedgerValidationError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{key}: {message}" for key, message in self.errors.items()))


def validate_entry_input(values: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    """利用者の入力を検証・正規化する。partial=True なら渡された項目だけを扱う。"""
    if not isinstance(values, dict):
        raise LedgerValidationError({"_": "入力の形式が正しくありません。"})

    errors: dict[str, str] = {}
    normalized: dict[str, Any] = {}

    unknown = sorted(key for key in values if key not in EDITABLE_FIELDS)
    for key in unknown:
        errors[key] = "変更できない項目です。"

    if not partial:
        for key in REQUIRED_FOR_CREATE:
            if _is_blank(values.get(key)):
                errors[key] = "入力してください。"

    for key in EDITABLE_FIELDS:
        if key not in values or key in errors:
            continue
        raw = values[key]
        try:
            normalized[key] = _normalize_field(key, raw)
        except ValueError as exc:
            errors[key] = str(exc)

    if not errors:
        amount = normalized.get("amount_yen")
        reimbursed = normalized.get("reimbursed_yen")
        if amount is not None and reimbursed is not None and reimbursed > amount:
            errors["reimbursed_yen"] = "補填される金額は支払った金額以下にしてください。"

    if errors:
        raise LedgerValidationError(errors)
    return normalized


def _normalize_field(key: str, raw: Any) -> Any:
    if key == "service_date":
        return _normalize_date(raw)
    if key == "provider_name":
        return _normalize_required_text(raw, MAX_PROVIDER_LENGTH)
    if key == "family_member_name":
        return _normalize_required_text(raw, MAX_FAMILY_NAME_LENGTH)
    if key == "amount_yen":
        amount = _normalize_yen(raw)
        if amount < 1:
            raise ValueError("1円以上を入力してください。")
        return amount
    if key == "reimbursed_yen":
        if _is_blank(raw):
            return 0
        return _normalize_yen(raw)
    if key == "medical_category":
        text = str(raw or "").strip()
        if text not in MEDICAL_CATEGORIES:
            raise ValueError("医療費の区分を選んでください。")
        return text
    if key == "note":
        text = str(raw or "").strip()
        if len(text) > MAX_NOTE_LENGTH:
            raise ValueError(f"{MAX_NOTE_LENGTH}文字以内で入力してください。")
        return text or None
    if key == "status":
        text = str(raw or "").strip()
        if text not in USER_SETTABLE_STATUSES:
            raise ValueError("状態の指定が正しくありません。")
        return text
    raise ValueError("変更できない項目です。")


def _normalize_date(raw: Any) -> str:
    if isinstance(raw, date):
        return raw.isoformat()
    text = str(raw or "").translate(_FULLWIDTH_DIGITS)
    match = _DATE_RE.match(text)
    if match is None:
        raise ValueError("日付は YYYY-MM-DD の形式で入力してください。")
    try:
        parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError as exc:
        raise ValueError("存在しない日付です。") from exc
    return parsed.isoformat()


def _normalize_required_text(raw: Any, max_length: int) -> str:
    text = " ".join(str(raw or "").replace("　", " ").split())
    if not text:
        raise ValueError("入力してください。")
    if len(text) > max_length:
        raise ValueError(f"{max_length}文字以内で入力してください。")
    return text


def _normalize_yen(raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError("金額は整数で入力してください。")
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError("金額は整数で入力してください。")
    text = str(raw).translate(_FULLWIDTH_DIGITS).replace(",", "").replace("円", "").replace("¥", "").strip()
    if not re.fullmatch(r"-?\d+(\.0+)?", text):
        raise ValueError("金額は整数で入力してください。")
    value = to_int(text.split(".")[0])
    if value is None:
        raise ValueError("金額は整数で入力してください。")
    if value < 0:
        raise ValueError("0円以上を入力してください。")
    if value > MAX_AMOUNT_YEN:
        raise ValueError("金額が大きすぎます。")
    return value


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９／－．，", "0123456789/-.,")

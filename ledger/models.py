from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

# 国税庁「医療費集計フォーム」の医療費の区分
CATEGORY_TREATMENT = "診療・治療"
CATEGORY_MEDICINE = "医薬品購入"
CATEGORY_CARE = "介護保険サービス"
CATEGORY_OTHER = "その他の医療費"
MEDICAL_CATEGORIES: tuple[str, ...] = (
    CATEGORY_TREATMENT,
    CATEGORY_MEDICINE,
    CATEGORY_CARE,
    CATEGORY_OTHER,
)

SOURCE_OCR = "ocr"
SOURCE_MANUAL = "manual"

STATUS_TENTATIVE = "tentative"
STATUS_HOLD = "hold"
STATUS_CONFIRMED = "confirmed"
ENTRY_STATUSES: tuple[str, ...] = (STATUS_TENTATIVE, STATUS_HOLD, STATUS_CONFIRMED)
# 年間・月間の集計に含める状態（既存の集計と同じ）
SUMMARY_STATUSES: frozenset[str] = frozenset({STATUS_TENTATIVE, STATUS_CONFIRMED})

MANUAL_ENTRY_PREFIX = "manual-"

_FULL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_DAY_RE = re.compile(r"^\d{2}-\d{2}$")

_PHARMACY_KEYWORDS = ("薬局", "ドラッグ", "調剤", "ファーマシー", "薬店", "薬品")
_CARE_KEYWORDS = ("介護", "デイサービス", "訪問看護", "ケアプラン", "老人保健")


@dataclass(slots=True)
class LedgerEntry:
    """申告資料の1行に相当する明細。entry_id は OCR 由来なら receipt_id と同じ。"""

    entry_id: str
    line_user_id: str
    source: str
    status: str
    service_date: str | None
    provider_name: str | None
    family_member_name: str | None
    amount_yen: int | None
    reimbursed_yen: int
    medical_category: str
    note: str | None
    created_at: str
    updated_at: str

    @property
    def is_manual(self) -> bool:
        return self.source == SOURCE_MANUAL

    def effective_date(self) -> str | None:
        """集計に使う日付（YYYY-MM-DD）。月日だけの日付は登録日の年で補う。"""
        return summary_date(self.service_date, self.created_at)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("line_user_id", None)
        data["effective_date"] = self.effective_date()
        return data

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "LedgerEntry":
        entry_id = str(row.get("receipt_id") or row.get("entry_id") or "")
        source = str(row.get("entry_source") or "").strip()
        if source not in (SOURCE_OCR, SOURCE_MANUAL):
            source = SOURCE_MANUAL if entry_id.startswith(MANUAL_ENTRY_PREFIX) else SOURCE_OCR
        provider = _clean_text(row.get("provider_name"))
        category = str(row.get("medical_category") or "").strip()
        if category not in MEDICAL_CATEGORIES:
            category = infer_medical_category(provider)
        return cls(
            entry_id=entry_id,
            line_user_id=str(row.get("line_user_id") or ""),
            source=source,
            status=str(row.get("status") or STATUS_TENTATIVE),
            service_date=_clean_text(row.get("service_date")),
            provider_name=provider,
            family_member_name=_clean_text(row.get("family_member_name")),
            amount_yen=to_int(row.get("amount_yen")),
            reimbursed_yen=to_int(row.get("reimbursed_yen")) or 0,
            medical_category=category,
            note=_clean_text(row.get("note")),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )


def infer_medical_category(provider_name: str | None) -> str:
    """支払先の名称から医療費の区分の初期値を推定する。利用者がいつでも変更できる前提の目安。"""
    text = str(provider_name or "")
    if any(keyword in text for keyword in _CARE_KEYWORDS):
        return CATEGORY_CARE
    if any(keyword in text for keyword in _PHARMACY_KEYWORDS):
        return CATEGORY_MEDICINE
    return CATEGORY_TREATMENT


def summary_date(service_date: Any, created_at: Any) -> str | None:
    date_text = str(service_date or "").strip()
    if _FULL_DATE_RE.match(date_text):
        return date_text
    created_text = str(created_at or "").strip()[:10]
    created_ok = bool(_FULL_DATE_RE.match(created_text))
    if _MONTH_DAY_RE.match(date_text):
        return f"{created_text[:4]}-{date_text}" if created_ok else None
    if date_text:
        return None
    return created_text if created_ok else None


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip()
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch == "-")
    if not cleaned or cleaned.count("-") > 1 or (cleaned.count("-") == 1 and not cleaned.startswith("-")):
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("　", " ").strip()
    return text or None

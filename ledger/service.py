from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from core.enums import FieldName
from inbox.repository_interface import InboxRepositoryProtocol
from ledger.checks import LedgerIssue, has_blocking_issues, run_checks
from ledger.providers import (
    alias_key,
    comparison_key,
    find_variant_groups,
    normalize_display_name,
    pair_key,
    provider_stats,
)
from ledger.models import (
    CATEGORY_OTHER,
    MANUAL_ENTRY_PREFIX,
    SOURCE_MANUAL,
    SOURCE_OCR,
    STATUS_CONFIRMED,
    LedgerEntry,
)
from ledger.tax_form import FormNotice, plan_form_rows, render_form
from ledger.validation import LedgerValidationError, validate_entry_input

# 明細を直したとき、OCR の項目値にも反映して再確定時に元へ戻らないようにする
_RECEIPT_FIELD_SYNC: dict[str, str] = {
    "service_date": FieldName.PAYMENT_DATE,
    "provider_name": FieldName.PAYER_FACILITY_NAME,
    "family_member_name": FieldName.FAMILY_MEMBER_NAME,
    "amount_yen": FieldName.PAYMENT_AMOUNT,
}


class LedgerEntryNotFoundError(LookupError):
    pass


class ReceiptImageNotFoundError(LookupError):
    """手入力の明細、または保存期間を過ぎて画像が消えている。"""


MAX_PROVIDER_NAMES_PER_REQUEST = 50


@dataclass(slots=True)
class DeleteResult:
    deleted: bool
    image_path: str | None = None


@dataclass(slots=True)
class LedgerSummary:
    year: int
    entry_count: int
    total_amount_yen: int
    total_reimbursed_yen: int
    unconfirmed_count: int
    by_family_member: dict[str, int]
    by_category: dict[str, int]

    @property
    def net_amount_yen(self) -> int:
        return self.total_amount_yen - self.total_reimbursed_yen

    def to_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "entry_count": self.entry_count,
            "total_amount_yen": self.total_amount_yen,
            "total_reimbursed_yen": self.total_reimbursed_yen,
            "net_amount_yen": self.net_amount_yen,
            "unconfirmed_count": self.unconfirmed_count,
            "by_family_member": dict(self.by_family_member),
            "by_category": dict(self.by_category),
        }


@dataclass(slots=True)
class LedgerCheckReport:
    summary: LedgerSummary
    issues: list[LedgerIssue]

    @property
    def ready_for_export(self) -> bool:
        return not has_blocking_issues(self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary.to_dict(),
            "issues": [issue.to_dict() for issue in self.issues],
            "ready_for_export": self.ready_for_export,
        }


@dataclass(slots=True)
class TaxFormExport:
    """申告用の「医療費集計フォーム」の内容。content は作成したときだけ入る。"""

    year: int
    included_count: int
    row_count: int
    total_amount_yen: int
    total_reimbursed_yen: int
    aggregated: bool
    excluded: list[dict[str, Any]]
    notices: list[FormNotice]
    content: bytes | None = None

    @property
    def filename(self) -> str:
        return f"iryouhi_shukei_{self.year}.xlsx"

    def to_dict(self) -> dict[str, Any]:
        excluded_counts: dict[str, int] = {}
        for item in self.excluded:
            excluded_counts[item["reason"]] = excluded_counts.get(item["reason"], 0) + 1
        return {
            "year": self.year,
            "included_count": self.included_count,
            "row_count": self.row_count,
            "total_amount_yen": self.total_amount_yen,
            "total_reimbursed_yen": self.total_reimbursed_yen,
            "net_amount_yen": self.total_amount_yen - self.total_reimbursed_yen,
            "aggregated": self.aggregated,
            "excluded": self.excluded,
            "excluded_counts": excluded_counts,
            "notices": [notice.to_dict() for notice in self.notices],
            "filename": self.filename,
        }


EXCLUDED_UNCONFIRMED = "unconfirmed"
EXCLUDED_HAS_ERROR = "has_error"


class LedgerService:
    def __init__(self, repository: InboxRepositoryProtocol) -> None:
        self.repository = repository

    def list_entries(self, line_user_id: str, year: int) -> list[LedgerEntry]:
        entries = [LedgerEntry.from_row(row) for row in self.repository.list_ledger_rows(line_user_id)]
        selected = [entry for entry in entries if entry_belongs_to_year(entry, year)]
        selected.sort(key=lambda entry: (entry.effective_date() or "9999", entry.created_at, entry.entry_id))
        return selected

    def get_entry(self, line_user_id: str, entry_id: str) -> LedgerEntry:
        row = self.repository.get_ledger_row(line_user_id, entry_id)
        if row is None:
            raise LedgerEntryNotFoundError(entry_id)
        return LedgerEntry.from_row(row)

    def create_manual_entry(self, line_user_id: str, values: dict[str, Any]) -> LedgerEntry:
        normalized = validate_entry_input(values, partial=False)
        normalized.setdefault("medical_category", CATEGORY_OTHER)
        normalized.setdefault("reimbursed_yen", 0)
        normalized.setdefault("status", STATUS_CONFIRMED)
        normalized["entry_source"] = SOURCE_MANUAL
        entry_id = f"{MANUAL_ENTRY_PREFIX}{uuid4()}"
        row = self.repository.save_ledger_row(line_user_id, entry_id, normalized)
        return LedgerEntry.from_row(row)

    def update_entry(self, line_user_id: str, entry_id: str, values: dict[str, Any]) -> LedgerEntry:
        current = self.get_entry(line_user_id, entry_id)
        patch = validate_entry_input(values, partial=True)
        if not patch:
            return current

        amount = patch.get("amount_yen", current.amount_yen)
        reimbursed = patch.get("reimbursed_yen", current.reimbursed_yen)
        if amount is not None and reimbursed is not None and reimbursed > amount:
            raise LedgerValidationError({"reimbursed_yen": "補填される金額は支払った金額以下にしてください。"})

        if current.source == SOURCE_OCR:
            for key, field_name in _RECEIPT_FIELD_SYNC.items():
                if key in patch:
                    self.repository.update_field_value(current.entry_id, field_name, patch[key], source="ledger_edit")

        row = self.repository.save_ledger_row(line_user_id, current.entry_id, patch)
        return LedgerEntry.from_row(row)

    def delete_entry(self, line_user_id: str, entry_id: str) -> DeleteResult:
        """明細を削除する。OCR 由来なら原本画像のパスを返すので、呼び出し側で画像も消す。"""
        current = self.get_entry(line_user_id, entry_id)
        if current.source == SOURCE_OCR:
            image_path = self.repository.delete_receipt(line_user_id=line_user_id, receipt_id=current.entry_id)
            if image_path is not None:
                return DeleteResult(deleted=True, image_path=image_path)
        removed = self.repository.delete_ledger_row(line_user_id, current.entry_id)
        # 画像パスのない OCR 明細は delete_receipt 側で既に消えているため、残っていないことで判定する
        return DeleteResult(deleted=removed or self.repository.get_ledger_row(line_user_id, current.entry_id) is None)

    def check(self, line_user_id: str, year: int) -> LedgerCheckReport:
        entries = self.list_entries(line_user_id, year)
        family_members = self.repository.list_family_members(line_user_id)
        return LedgerCheckReport(
            summary=summarize_entries(entries, year),
            issues=run_checks(
                entries,
                family_members,
                self.repository.list_provider_distinct_pair_keys(line_user_id),
            ),
        )

    # --- 申告用資料（国税庁「医療費集計フォーム」） ---

    def prepare_tax_form(self, line_user_id: str, year: int, *, render: bool = False) -> TaxFormExport:
        """対象年の、確定済みで要修正の問題がない明細をフォームにする。含めなかった明細は理由を返す。"""
        report = self.check(line_user_id, year)
        entries = self.list_entries(line_user_id, year)
        error_ids = {issue.entry_id for issue in report.issues if issue.severity == "error"}
        included: list[LedgerEntry] = []
        excluded: list[dict[str, Any]] = []
        for entry in entries:
            if entry.status != STATUS_CONFIRMED:
                excluded.append({"entry_id": entry.entry_id, "reason": EXCLUDED_UNCONFIRMED})
            elif entry.entry_id in error_ids:
                excluded.append({"entry_id": entry.entry_id, "reason": EXCLUDED_HAS_ERROR})
            else:
                included.append(entry)
        plan = plan_form_rows(included)
        return TaxFormExport(
            year=year,
            included_count=len(included),
            row_count=len(plan.rows),
            total_amount_yen=plan.total_amount_yen,
            total_reimbursed_yen=plan.total_reimbursed_yen,
            aggregated=plan.aggregated,
            excluded=excluded,
            notices=plan.notices,
            content=render_form(plan.rows) if render and plan.rows else None,
        )

    # --- 支払先の名寄せ ---

    def provider_overview(self, line_user_id: str, year: int) -> dict[str, Any]:
        stats = provider_stats(self.list_entries(line_user_id, year))
        groups = find_variant_groups(stats, self.repository.list_provider_distinct_pair_keys(line_user_id))
        return {
            "year": year,
            "providers": [stat.to_dict() for stat in stats],
            "groups": [group.to_dict() for group in groups],
        }

    def known_provider_names(self, line_user_id: str) -> list[str]:
        """全年の支払先の名前（よく使う順）。入力欄の候補に使う。"""
        entries = [LedgerEntry.from_row(row) for row in self.repository.list_ledger_rows(line_user_id)]
        return [stat.name for stat in provider_stats(entries)]

    def rename_providers(self, line_user_id: str, from_names: list[str], to_name: str) -> int:
        """支払先の名前をそろえる（全年の明細が対象）。次回の OCR でも同じ名前に置き換えるよう記録する。"""
        target = validate_entry_input({"provider_name": to_name}, partial=True)["provider_name"]
        sources = _clean_names(from_names)
        if not sources:
            raise LedgerValidationError({"from_names": "そろえる名前を選んでください。"})

        updated = 0
        for row in self.repository.list_ledger_rows(line_user_id):
            entry = LedgerEntry.from_row(row)
            if entry.provider_name not in sources or entry.provider_name == target:
                continue
            self.update_entry(line_user_id, entry.entry_id, {"provider_name": target})
            updated += 1
        for name in sources:
            if comparison_key(name) and name != target:
                self.repository.upsert_provider_alias(line_user_id, alias_key(name), name, target)
        return updated

    def mark_providers_distinct(self, line_user_id: str, names: list[str]) -> int:
        """「別の支払先です」。指定した名前どうしを、今後は表記ゆれの候補にしない。"""
        cleaned = _clean_names(names)
        if len(cleaned) < 2:
            raise LedgerValidationError({"names": "2つ以上の名前を指定してください。"})
        pairs = [
            (pair_key(a, b), a, b)
            for index, a in enumerate(cleaned)
            for b in cleaned[index + 1:]
        ]
        self.repository.add_provider_distinct_pairs(line_user_id, pairs)
        return len(pairs)

    def receipt_image_path(self, line_user_id: str, entry_id: str) -> str:
        entry = self.get_entry(line_user_id, entry_id)
        if entry.source != SOURCE_OCR:
            raise ReceiptImageNotFoundError(entry_id)
        path = self.repository.get_receipt_image_path(line_user_id, entry.entry_id)
        if not path:
            raise ReceiptImageNotFoundError(entry_id)
        return path


def apply_provider_alias(repository: InboxRepositoryProtocol, line_user_id: str, name: Any) -> str | None:
    """OCR で読み取った支払先の名前を、利用者が以前そろえた名前に置き換える。該当がなければ None。"""
    display = normalize_display_name(name)
    if not display or not comparison_key(display):
        return None
    canonical = repository.get_provider_alias(line_user_id, alias_key(display))
    if not canonical or canonical == display:
        return None
    return canonical


def _clean_names(names: Any) -> list[str]:
    if not isinstance(names, list):
        return []
    cleaned: list[str] = []
    for name in names[:MAX_PROVIDER_NAMES_PER_REQUEST]:
        text = str(name or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def entry_belongs_to_year(entry: LedgerEntry, year: int) -> bool:
    """支払日が分かればその年、分からなければ登録日の年で振り分ける（日付不明の明細も一覧に出すため）。"""
    effective = entry.effective_date()
    reference = effective or entry.created_at[:10]
    return reference.startswith(f"{year:04d}-")


def summarize_entries(entries: list[LedgerEntry], year: int) -> LedgerSummary:
    total = 0
    reimbursed = 0
    unconfirmed = 0
    by_family: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for entry in entries:
        amount = entry.amount_yen or 0
        total += amount
        reimbursed += entry.reimbursed_yen
        if entry.status != STATUS_CONFIRMED:
            unconfirmed += 1
        family = entry.family_member_name or "（未入力）"
        by_family[family] = by_family.get(family, 0) + amount
        by_category[entry.medical_category] = by_category.get(entry.medical_category, 0) + amount
    return LedgerSummary(
        year=year,
        entry_count=len(entries),
        total_amount_yen=total,
        total_reimbursed_yen=reimbursed,
        unconfirmed_count=unconfirmed,
        by_family_member=by_family,
        by_category=by_category,
    )

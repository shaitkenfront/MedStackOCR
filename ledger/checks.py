from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ledger.models import SOURCE_OCR, STATUS_CONFIRMED, LedgerEntry
from ledger.providers import find_variant_groups, provider_stats

SEVERITY_ERROR = "error"  # 申告資料に載せる前に直す必要がある
SEVERITY_WARNING = "warning"  # 確認を勧める

CODE_UNCONFIRMED = "unconfirmed"
CODE_MISSING_DATE = "missing_date"
CODE_INCOMPLETE_DATE = "incomplete_date"
CODE_MISSING_PROVIDER = "missing_provider"
CODE_MISSING_FAMILY = "missing_family_member"
CODE_MISSING_AMOUNT = "missing_amount"
CODE_REIMBURSED_EXCEEDS = "reimbursed_exceeds_amount"
CODE_DUPLICATE = "duplicate_candidate"
CODE_UNKNOWN_FAMILY = "unknown_family_member"
CODE_YEAR_DIFFERS = "year_differs_from_registration"
CODE_PROVIDER_VARIANT = "provider_name_variant"
CODE_PROVIDER_SUSPICIOUS = "provider_name_suspicious"
CODE_FORM_TEXT_TOO_LONG = "form_text_too_long"

# 国税庁「医療費集計フォーム」の文字数制限（ledger.tax_form と同じ値）
FORM_MAX_FAMILY_NAME_LENGTH = 10
FORM_MAX_PROVIDER_NAME_LENGTH = 20


@dataclass(slots=True)
class LedgerIssue:
    entry_id: str
    code: str
    severity: str
    message: str
    related_entry_ids: list[str] = field(default_factory=list)
    group_id: str | None = None  # 支払先の表記ゆれの組（支払先タブで開くため）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_checks(
    entries: Iterable[LedgerEntry],
    family_members: Iterable[dict[str, Any]] = (),
    distinct_provider_pair_keys: Iterable[str] = (),
) -> list[LedgerIssue]:
    """明細ごとの抜け漏れ・入力ミスの候補を返す。entries は対象年の明細を渡す。"""
    entry_list = list(entries)
    known_names = _known_family_names(family_members)
    issues: list[LedgerIssue] = []

    for entry in entry_list:
        issues.extend(_check_entry(entry, known_names))
    issues.extend(_check_duplicates(entry_list))
    issues.extend(_check_provider_names(entry_list, distinct_provider_pair_keys))

    order = {entry.entry_id: index for index, entry in enumerate(entry_list)}
    severity_rank = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1}
    issues.sort(key=lambda issue: (severity_rank.get(issue.severity, 9), order.get(issue.entry_id, 0), issue.code))
    return issues


def has_blocking_issues(issues: Iterable[LedgerIssue]) -> bool:
    return any(issue.severity == SEVERITY_ERROR for issue in issues)


def _check_entry(entry: LedgerEntry, known_names: set[str]) -> list[LedgerIssue]:
    issues: list[LedgerIssue] = []

    def add(code: str, severity: str, message: str) -> None:
        issues.append(LedgerIssue(entry_id=entry.entry_id, code=code, severity=severity, message=message))

    if entry.status != STATUS_CONFIRMED:
        add(CODE_UNCONFIRMED, SEVERITY_WARNING, "内容が確定していません。確認して確定してください。")

    if not entry.service_date:
        add(CODE_MISSING_DATE, SEVERITY_ERROR, "支払日が入っていません。")
    elif entry.effective_date() is None:
        add(CODE_INCOMPLETE_DATE, SEVERITY_ERROR, "支払日の形式が正しくありません。日付を入力し直してください。")
    elif entry.effective_date() != entry.service_date:
        add(CODE_INCOMPLETE_DATE, SEVERITY_WARNING, "支払日の年が読み取れていません。年を確認してください。")

    if not entry.provider_name:
        add(CODE_MISSING_PROVIDER, SEVERITY_ERROR, "病院・薬局などの支払先が入っていません。")
    if not entry.family_member_name:
        add(CODE_MISSING_FAMILY, SEVERITY_ERROR, "医療を受けた人が入っていません。")
    elif known_names and _normalize_name(entry.family_member_name) not in known_names:
        add(CODE_UNKNOWN_FAMILY, SEVERITY_WARNING, "家族として登録されていない名前です。表記を確認してください。")

    for value, limit, label in (
        (entry.provider_name, FORM_MAX_PROVIDER_NAME_LENGTH, "支払先の名前"),
        (entry.family_member_name, FORM_MAX_FAMILY_NAME_LENGTH, "医療を受けた人の名前"),
    ):
        if value and len(_form_text(value)) > limit:
            add(
                CODE_FORM_TEXT_TOO_LONG,
                SEVERITY_WARNING,
                f"{label}が申告用フォームの上限（{limit}文字）を超えています。そのままだと先頭の{limit}文字だけが書き出されます。",
            )

    if entry.amount_yen is None or entry.amount_yen <= 0:
        add(CODE_MISSING_AMOUNT, SEVERITY_ERROR, "支払った金額が入っていません。")
    elif entry.reimbursed_yen > entry.amount_yen:
        add(CODE_REIMBURSED_EXCEEDS, SEVERITY_ERROR, "補填される金額が支払った金額を超えています。")

    effective = entry.effective_date()
    if (
        entry.source == SOURCE_OCR
        and effective is not None
        and entry.service_date == effective
        and len(entry.created_at) >= 4
        and effective[:4] != entry.created_at[:4]
    ):
        add(CODE_YEAR_DIFFERS, SEVERITY_WARNING, "支払日の年が登録した年と違います。読み取り誤りがないか確認してください。")
    return issues


def _check_duplicates(entries: list[LedgerEntry]) -> list[LedgerIssue]:
    groups: dict[tuple[str, str, str, int], list[LedgerEntry]] = defaultdict(list)
    for entry in entries:
        effective = entry.effective_date()
        if not effective or not entry.provider_name or not entry.family_member_name or not entry.amount_yen:
            continue
        key = (
            effective,
            _normalize_name(entry.provider_name),
            _normalize_name(entry.family_member_name),
            entry.amount_yen,
        )
        groups[key].append(entry)

    issues: list[LedgerIssue] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        ids = [entry.entry_id for entry in group]
        for entry in group:
            issues.append(
                LedgerIssue(
                    entry_id=entry.entry_id,
                    code=CODE_DUPLICATE,
                    severity=SEVERITY_WARNING,
                    message="日付・支払先・対象者・金額が同じ明細があります。二重登録でないか確認してください。",
                    related_entry_ids=[other for other in ids if other != entry.entry_id],
                )
            )
    return issues


def _check_provider_names(entries: list[LedgerEntry], distinct_pair_keys: Iterable[str]) -> list[LedgerIssue]:
    stats = provider_stats(entries)
    issues: list[LedgerIssue] = []
    for stat in stats:
        if stat.suspicious_reason is None:
            continue
        for entry_id in stat.entry_ids:
            issues.append(
                LedgerIssue(
                    entry_id=entry_id,
                    code=CODE_PROVIDER_SUSPICIOUS,
                    severity=SEVERITY_WARNING,
                    message=f"支払先の名前に読み取りミスの疑いがあります（{stat.suspicious_reason}）。",
                )
            )
    for group in find_variant_groups(stats, distinct_pair_keys):
        for stat in group.names:
            if stat.name == group.suggested_name:
                continue
            for entry_id in stat.entry_ids:
                issues.append(
                    LedgerIssue(
                        entry_id=entry_id,
                        code=CODE_PROVIDER_VARIANT,
                        severity=SEVERITY_WARNING,
                        message=f"支払先「{group.suggested_name}」と表記が似ています。同じ支払先なら名前をそろえてください。",
                        group_id=group.group_id,
                    )
                )
    return issues


def _known_family_names(family_members: Iterable[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for member in family_members:
        if not isinstance(member, dict):
            continue
        for value in [member.get("canonical_name"), *(member.get("aliases") or [])]:
            normalized = _normalize_name(value)
            if normalized:
                names.add(normalized)
    return names


def _form_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _normalize_name(value: Any) -> str:
    text = str(value or "").replace("　", " ").strip().lower()
    return "".join(ch for ch in text if not ch.isspace())

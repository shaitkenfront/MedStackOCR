"""国税庁「医療費集計フォーム」（Ver.3.1）への書き出し。

確定申告書等作成コーナーは、入力規則やシートの保護を外したフォームを読み込めないことがある。
そのため Excel ライブラリで開いて保存し直すのではなく、入力欄のセルだけを XML で書き換え、
それ以外のファイル（入力規則・保護・画像・メタデータ）はバイト単位でそのままコピーする。
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

from ledger.models import (
    CATEGORY_CARE,
    CATEGORY_MEDICINE,
    CATEGORY_OTHER,
    CATEGORY_TREATMENT,
    LedgerEntry,
)

TEMPLATE_PATH = Path(__file__).resolve().parent / "forms" / "iryouhi_form_v3.1.xlsx"
FORM_VERSION = "3.1"
SHEET_PART = "xl/worksheets/sheet1.xml"
SHARED_STRINGS_PART = "xl/sharedStrings.xml"
WORKBOOK_PART = "xl/workbook.xml"

FIRST_ROW = 9
MAX_ROWS = 995  # 9〜1003 行目
MAX_FAMILY_NAME_LENGTH = 10
MAX_PROVIDER_NAME_LENGTH = 20
MAX_AMOUNT = 999_999_999
APPLICABLE_TEXT = "該当する"

CATEGORY_COLUMNS = {
    CATEGORY_TREATMENT: "D",
    CATEGORY_MEDICINE: "E",
    CATEGORY_CARE: "F",
    CATEGORY_OTHER: "G",
}


class TaxFormError(RuntimeError):
    pass


@dataclass(slots=True)
class FormRow:
    family_member_name: str
    provider_name: str
    medical_category: str
    amount_yen: int
    reimbursed_yen: int
    payment_date: str | None  # YYYY/MM/DD
    entry_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FormNotice:
    """書き出しのときに、内容を変えた・含めなかった明細の知らせ。"""

    code: str
    message: str
    entry_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "entry_ids": list(self.entry_ids)}


@dataclass(slots=True)
class FormPlan:
    rows: list[FormRow]
    notices: list[FormNotice]
    aggregated: bool

    @property
    def total_amount_yen(self) -> int:
        return sum(row.amount_yen for row in self.rows)

    @property
    def total_reimbursed_yen(self) -> int:
        return sum(row.reimbursed_yen for row in self.rows)


def plan_form_rows(entries: Iterable[LedgerEntry]) -> FormPlan:
    """明細をフォームの行にする。995 件を超える場合は「医療を受けた人・支払先・区分」ごとにまとめる。"""
    rows: list[FormRow] = []
    notices: list[FormNotice] = []
    truncated_family: list[str] = []
    truncated_provider: list[str] = []
    for entry in entries:
        family = _form_text(entry.family_member_name)
        provider = _form_text(entry.provider_name)
        if len(family) > MAX_FAMILY_NAME_LENGTH:
            family = family[:MAX_FAMILY_NAME_LENGTH]
            truncated_family.append(entry.entry_id)
        if len(provider) > MAX_PROVIDER_NAME_LENGTH:
            provider = provider[:MAX_PROVIDER_NAME_LENGTH]
            truncated_provider.append(entry.entry_id)
        date = entry.effective_date()
        rows.append(
            FormRow(
                family_member_name=family,
                provider_name=provider,
                medical_category=entry.medical_category,
                amount_yen=int(entry.amount_yen or 0),
                reimbursed_yen=int(entry.reimbursed_yen or 0),
                payment_date=date.replace("-", "/") if date else None,
                entry_ids=[entry.entry_id],
            )
        )
    if truncated_family:
        notices.append(
            FormNotice(
                "family_name_truncated",
                f"医療を受けた人の名前が{MAX_FAMILY_NAME_LENGTH}文字を超えるため、先頭の{MAX_FAMILY_NAME_LENGTH}文字にしました。",
                truncated_family,
            )
        )
    if truncated_provider:
        notices.append(
            FormNotice(
                "provider_name_truncated",
                f"支払先の名前が{MAX_PROVIDER_NAME_LENGTH}文字を超えるため、先頭の{MAX_PROVIDER_NAME_LENGTH}文字にしました。",
                truncated_provider,
            )
        )

    rows.sort(key=lambda row: (row.payment_date or "9999", row.family_member_name, row.provider_name))
    aggregated = False
    if len(rows) > MAX_ROWS:
        rows = _aggregate(rows)
        aggregated = True
        notices.append(
            FormNotice(
                "aggregated",
                f"明細が{MAX_ROWS}件を超えるため、「医療を受けた人・支払先・区分」ごとに合計してまとめました（支払年月日は空欄）。",
            )
        )
        if len(rows) > MAX_ROWS:
            raise TaxFormError(f"まとめても {len(rows)} 行になり、フォームの上限（{MAX_ROWS} 行）を超えます。")
    for row in rows:
        if row.amount_yen > MAX_AMOUNT or row.reimbursed_yen > MAX_AMOUNT:
            raise TaxFormError("1行の金額がフォームの上限（999,999,999円）を超えています。")
    return FormPlan(rows=rows, notices=notices, aggregated=aggregated)


def render_form(rows: list[FormRow], template_path: Path = TEMPLATE_PATH) -> bytes:
    """フォームのひな形に行を書き込んだ xlsx のバイト列を返す。"""
    if len(rows) > MAX_ROWS:
        raise TaxFormError(f"フォームに書き込めるのは {MAX_ROWS} 行までです。")
    with zipfile.ZipFile(template_path) as template:
        sheet_xml = template.read(SHEET_PART).decode("utf-8")
        shared_xml = template.read(SHARED_STRINGS_PART).decode("utf-8")
        workbook_xml = template.read(WORKBOOK_PART).decode("utf-8")

        strings = _SharedStrings(shared_xml)
        applicable_index = strings.index_of(APPLICABLE_TEXT)
        for offset, row in enumerate(rows):
            row_number = FIRST_ROW + offset
            values: dict[str, str] = {
                "B": _string_cell(strings.add(row.family_member_name)),
                "C": _string_cell(strings.add(row.provider_name)),
                "H": _number_cell(row.amount_yen),
            }
            column = CATEGORY_COLUMNS.get(row.medical_category, CATEGORY_COLUMNS[CATEGORY_OTHER])
            values[column] = _string_cell(applicable_index)
            if row.reimbursed_yen:
                values["I"] = _number_cell(row.reimbursed_yen)
            if row.payment_date:
                values["J"] = _string_cell(strings.add(row.payment_date))
            sheet_xml = _fill_row(sheet_xml, row_number, values)

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as result:
            for info in template.infolist():
                if info.filename == SHEET_PART:
                    data = sheet_xml.encode("utf-8")
                elif info.filename == SHARED_STRINGS_PART:
                    data = strings.to_xml().encode("utf-8")
                elif info.filename == WORKBOOK_PART:
                    data = _enable_recalculation(workbook_xml).encode("utf-8")
                else:
                    data = template.read(info.filename)
                result.writestr(info, data, compress_type=info.compress_type)
    return output.getvalue()


# ---------------------------------------------------------------- helpers


def _form_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    return " ".join(text.split())


def _aggregate(rows: list[FormRow]) -> list[FormRow]:
    grouped: dict[tuple[str, str, str], FormRow] = {}
    for row in rows:
        key = (row.family_member_name, row.provider_name, row.medical_category)
        target = grouped.get(key)
        if target is None:
            grouped[key] = FormRow(row.family_member_name, row.provider_name, row.medical_category, 0, 0, None, [])
            target = grouped[key]
        target.amount_yen += row.amount_yen
        target.reimbursed_yen += row.reimbursed_yen
        target.entry_ids.extend(row.entry_ids)
    return sorted(grouped.values(), key=lambda row: (row.family_member_name, row.provider_name, row.medical_category))


def _string_cell(index: int) -> str:
    return f't="s"><v>{index}</v>'


def _number_cell(value: int) -> str:
    return f"><v>{int(value)}</v>"


def _fill_row(sheet_xml: str, row_number: int, values: dict[str, str]) -> str:
    row_match = re.search(rf'<row r="{row_number}"[^>]*>.*?</row>', sheet_xml, re.S)
    if row_match is None:
        raise TaxFormError(f"フォームの {row_number} 行目が見つかりません（様式が変わった可能性があります）。")
    row_xml = row_match.group(0)
    for column, content in values.items():
        ref = f"{column}{row_number}"
        cell = re.search(rf'<c r="{ref}"( s="\d+")?\s*/>', row_xml)
        if cell is None:
            raise TaxFormError(f"フォームの {ref} セルが空欄ではありません（様式が変わった可能性があります）。")
        style = cell.group(1) or ""
        row_xml = row_xml.replace(cell.group(0), f'<c r="{ref}"{style} {content}</c>'.replace(" >", ">"), 1)
    return sheet_xml[: row_match.start()] + row_xml + sheet_xml[row_match.end():]


def _enable_recalculation(workbook_xml: str) -> str:
    """合計欄（C3・C4）を開いたときに再計算させる。"""
    if "fullCalcOnLoad" in workbook_xml:
        return workbook_xml
    return re.sub(r"<calcPr\b", '<calcPr fullCalcOnLoad="1"', workbook_xml, count=1)


class _SharedStrings:
    def __init__(self, xml: str) -> None:
        match = re.search(r"<sst\b[^>]*>", xml)
        if match is None:
            raise TaxFormError("sharedStrings.xml の形式が想定と違います。")
        self._xml = xml
        self._existing = re.findall(r"<si>(.*?)</si>", xml, re.S)
        self._texts = ["".join(re.findall(r"<t[^>]*>(.*?)</t>", item, re.S)) for item in self._existing]
        self._added: list[str] = []
        self._index: dict[str, int] = {}
        self._references = 0

    def index_of(self, text: str) -> int:
        escaped = escape(text)
        for index, value in enumerate(self._texts):
            if value == escaped:
                self._references += 1
                return index
        return self.add(text)

    def add(self, text: str) -> int:
        self._references += 1
        if text in self._index:
            return self._index[text]
        index = len(self._existing) + len(self._added)
        self._added.append(text)
        self._index[text] = index
        return index

    def to_xml(self) -> str:
        unique = len(self._existing) + len(self._added)
        count_match = re.search(r'\bcount="(\d+)"', self._xml)
        count = (int(count_match.group(1)) if count_match else len(self._existing)) + self._references
        xml = re.sub(r'\bcount="\d+"', f'count="{count}"', self._xml, count=1)
        xml = re.sub(r'\buniqueCount="\d+"', f'uniqueCount="{unique}"', xml, count=1)
        additions = "".join(f'<si><t xml:space="preserve">{escape(text)}</t></si>' for text in self._added)
        return xml.replace("</sst>", additions + "</sst>", 1)

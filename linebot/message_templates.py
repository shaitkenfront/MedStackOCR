from __future__ import annotations

from typing import Any

from core.enums import FieldName
from linebot.quick_replies import message_action, postback_action, with_quick_reply

FIELD_LABELS = {
    FieldName.PAYER_FACILITY_NAME: "医療機関",
    FieldName.PAYMENT_DATE: "日付",
    FieldName.PAYMENT_AMOUNT: "金額",
    FieldName.FAMILY_MEMBER_NAME: "対象者",
}

EDITABLE_FIELDS = (
    FieldName.PAYER_FACILITY_NAME,
    FieldName.PAYMENT_DATE,
    FieldName.PAYMENT_AMOUNT,
    FieldName.FAMILY_MEMBER_NAME,
)

FAMILY_REGISTRATION_FINISH_TEXT = "家族氏名の登録を終了"


def _text(value: Any) -> str:
    if value in (None, ""):
        return "-"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    return str(value)


def _field_text(field_name: str, value: Any) -> str:
    value_text = _text(value)
    if field_name == FieldName.PAYMENT_AMOUNT and value_text != "-":
        return f"{value_text}円"
    return value_text


def _summary_lines(fields: dict[str, Any]) -> list[str]:
    return [
        f"医療機関: {_field_text(FieldName.PAYER_FACILITY_NAME, fields.get(FieldName.PAYER_FACILITY_NAME))}",
        f"日付: {_field_text(FieldName.PAYMENT_DATE, fields.get(FieldName.PAYMENT_DATE))}",
        f"金額: {_field_text(FieldName.PAYMENT_AMOUNT, fields.get(FieldName.PAYMENT_AMOUNT))}",
        f"対象者: {_field_text(FieldName.FAMILY_MEMBER_NAME, fields.get(FieldName.FAMILY_MEMBER_NAME))}",
    ]


def build_processing_message() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "画像を受け付けました。読み取り中です。"}]


def build_auto_accept_message(receipt_id: str, fields: dict[str, Any]) -> list[dict[str, Any]]:
    actions = [
        postback_action("修正する", f"a=edit&r={receipt_id}", "修正する"),
        postback_action("保留", f"a=hold&r={receipt_id}", "保留"),
    ]
    text = "登録しました。\n" + "\n".join(_summary_lines(fields))
    return [with_quick_reply(text, actions)]


def build_review_required_message(receipt_id: str, fields: dict[str, Any]) -> list[dict[str, Any]]:
    actions = [
        postback_action("この内容で確定", f"a=ok&r={receipt_id}", "確定"),
        postback_action("修正する", f"a=edit&r={receipt_id}", "修正する"),
        postback_action("保留", f"a=hold&r={receipt_id}", "保留"),
    ]
    text = "確認が必要です。内容を確認してください。\n" + "\n".join(_summary_lines(fields))
    return [with_quick_reply(text, actions)]


def build_rejected_message() -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": "読み取りに失敗しました。明るい場所で真上から再撮影してください。",
        }
    ]


def build_choose_field_message(receipt_id: str) -> list[dict[str, Any]]:
    actions = [
        postback_action("医療機関", f"a=field&r={receipt_id}&f={FieldName.PAYER_FACILITY_NAME}", "医療機関"),
        postback_action("日付", f"a=field&r={receipt_id}&f={FieldName.PAYMENT_DATE}", "日付"),
        postback_action("金額", f"a=field&r={receipt_id}&f={FieldName.PAYMENT_AMOUNT}", "金額"),
        postback_action("対象者", f"a=field&r={receipt_id}&f={FieldName.FAMILY_MEMBER_NAME}", "対象者"),
        postback_action("戻る", f"a=back&r={receipt_id}", "戻る"),
    ]
    return [with_quick_reply("修正する項目を選択してください。", actions)]


def build_choose_candidate_message(
    receipt_id: str,
    field_name: str,
    candidates: list[Any],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates[:3]):
        label = _field_text(field_name, candidate)
        actions.append(
            postback_action(
                label if len(label) <= 20 else f"候補{idx + 1}",
                f"a=pick&r={receipt_id}&f={field_name}&i={idx}",
                label,
            )
        )
    actions.extend(
        [
            postback_action("手入力", f"a=free_text&r={receipt_id}&f={field_name}", "手入力"),
            postback_action("戻る", f"a=back&r={receipt_id}", "戻る"),
        ]
    )
    field_label = FIELD_LABELS.get(field_name, field_name)
    return [with_quick_reply(f"{field_label}の候補を選択してください。", actions)]


def build_field_updated_message(receipt_id: str, fields: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    label = FIELD_LABELS.get(field_name, field_name)
    value = _field_text(field_name, fields.get(field_name))
    actions = [
        postback_action("この内容で確定", f"a=ok&r={receipt_id}", "確定"),
        postback_action("別の項目を修正", f"a=edit&r={receipt_id}", "修正する"),
        postback_action("保留", f"a=hold&r={receipt_id}", "保留"),
    ]
    text = f"{label}を更新しました: {value}\n" + "\n".join(_summary_lines(fields))
    return [with_quick_reply(text, actions)]


def build_hold_message() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "保留にしました。後で「未確認」で確認できます。"}]


def build_confirmed_message(fields: dict[str, Any]) -> list[dict[str, Any]]:
    text = "確定しました。\n" + "\n".join(_summary_lines(fields))
    return [{"type": "text", "text": text}]


def build_cancelled_message() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "操作をキャンセルしました。"}]


def build_help_message() -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": (
                "使い方:\n"
                "- 領収書画像を送信\n"
                "- 必要なら候補を選んで修正\n"
                "- コマンド: 今年の医療費 / 今月の医療費 / 未確認 / ヘルプ"
            ),
        }
    ]


def build_unknown_message() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "操作が分かりませんでした。必要なら「ヘルプ」と入力してください。"}]


def build_aggregate_message(title: str, total: int, count: int, pending: int = 0) -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": (
                f"{title}\n"
                f"件数: {count}\n"
                f"合計: {total:,}円\n"
                f"未確認: {pending}件"
            ),
        }
    ]


def build_yearly_cumulative_message(year_totals: list[tuple[int, int]]) -> list[dict[str, Any]]:
    lines: list[str] = []
    for year, total in year_totals:
        lines.append(f"{year}年の累計医療費: {total:,}円")
    if not lines:
        lines.append("今年の累計医療費: 0円")
    return [{"type": "text", "text": "\n".join(lines)}]


def _family_registration_actions() -> list[dict[str, Any]]:
    return [message_action(FAMILY_REGISTRATION_FINISH_TEXT, FAMILY_REGISTRATION_FINISH_TEXT)]


def build_family_registration_prompt_message() -> list[dict[str, Any]]:
    text = (
        "ご家族の名前を教えてください。"
        "カタカナや、良く間違えられる漢字も登録しておくと認識の精度が上ります。\n"
        "1行に1名、別表記は「,」区切りで登録できます。\n"
        "例: 山田 太郎, ヤマダ タロウ, 山田太朗\n"
        "登録が終わったら「家族氏名の登録を終了」を押してください。"
    )
    return [with_quick_reply(text, _family_registration_actions())]


def build_family_registration_saved_message(total_members: int, latest: list[str]) -> list[dict[str, Any]]:
    preview = "、".join(latest[:3])
    head = f"{preview} を登録しました。\n" if preview else ""
    text = (
        f"{head}登録済み: {total_members}名\n"
        "続けて名前を送るか、「家族氏名の登録を終了」を押してください。"
    )
    return [with_quick_reply(text, _family_registration_actions())]


def build_family_registration_need_member_message() -> list[dict[str, Any]]:
    text = (
        "家族氏名が未登録です。最低1名は登録してください。\n"
        "登録が終わったら「家族氏名の登録を終了」を押してください。"
    )
    return [with_quick_reply(text, _family_registration_actions())]


def build_family_registration_completed_message(total_members: int) -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": f"家族氏名の登録が完了しました（{total_members}名）。領収書画像を送信してください。",
        }
    ]

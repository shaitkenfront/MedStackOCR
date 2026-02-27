from __future__ import annotations

import re
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

FAMILY_REGISTRATION_FINISH_TEXT = "名前登録を終了する"
FAMILY_REGISTRATION_FINISH_TEXT_LEGACY = "家族氏名の登録を終了"
FAMILY_REGISTRATION_NEXT_TEXT = "次の名前の入力に進む"
FAMILY_REGISTRATION_SKIP_TEXT = "該当なし"
_ISO_DATE_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")
_SHORT_DATE_RE = re.compile(r"^(?P<month>\d{2})-(?P<day>\d{2})$")


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
    if field_name == FieldName.PAYMENT_DATE and value_text != "-":
        full_match = _ISO_DATE_RE.match(value_text)
        if full_match is not None:
            return (
                f"{full_match.group('year')}/"
                f"{full_match.group('month')}/"
                f"{full_match.group('day')}"
            )
        short_match = _SHORT_DATE_RE.match(value_text)
        if short_match is not None:
            return f"{short_match.group('month')}/{short_match.group('day')}"
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
        postback_action("この内容で確定", f"a=ok&r={receipt_id}", "確定"),
        postback_action("修正する", f"a=edit&r={receipt_id}", "修正する"),
        postback_action("保留", f"a=hold&r={receipt_id}", "保留"),
        message_action("取り消し", "取り消し"),
    ]
    text = "内容を確認してください。\n" + "\n".join(_summary_lines(fields))
    return [with_quick_reply(text, actions)]


def build_review_required_message(receipt_id: str, fields: dict[str, Any]) -> list[dict[str, Any]]:
    actions = [
        postback_action("この内容で確定", f"a=ok&r={receipt_id}", "確定"),
        postback_action("修正する", f"a=edit&r={receipt_id}", "修正する"),
        postback_action("保留", f"a=hold&r={receipt_id}", "保留"),
        message_action("取り消し", "取り消し"),
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


def build_ocr_unavailable_message() -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": "現在OCR処理を実行できません。時間をおいて再送してください。",
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
    include_add_family_action: bool = False,
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
    if include_add_family_action and field_name == FieldName.FAMILY_MEMBER_NAME:
        actions.append(
            postback_action("新しい家族を追加", f"a=add_family&r={receipt_id}", "新しい家族を追加")
        )
    actions.extend(
        [
            postback_action("自分で入力する", f"a=free_text&r={receipt_id}&f={field_name}", "自分で入力する"),
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


def build_payment_date_need_year_message() -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": "年が省略されています。候補から年を選択してください。",
        }
    ]


def build_payment_date_invalid_message() -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": (
                "日付は年・月・日がそろう形式で入力してください。\n"
                "例: 2026-02-03 / 2026年2月3日 / 令和8年2月3日"
            ),
        }
    ]


def build_hold_message() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "保留にしました。後で「未確認」で確認できます。"}]


def build_confirmed_message(fields: dict[str, Any]) -> list[dict[str, Any]]:
    text = "以下の内容で登録しました。\n" + "\n".join(_summary_lines(fields))
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
                "- コマンド: 今年の医療費 / 今月の医療費 / 未確認 / 取り消し / ヘルプ\n"
                "- 短時間の連続送信時は受付を一時制限します"
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


def build_duplicate_warning_message(receipt_id: str, duplicates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    count = len(duplicates)
    reasons: set[str] = set()
    for item in duplicates:
        for reason in item.get("reasons", []):
            reasons.add(str(reason))
    reason_labels: list[str] = []
    if "image_sha256" in reasons:
        reason_labels.append("同一画像")
    if "fields" in reasons:
        reason_labels.append("日付・医療機関・対象者・金額")
    reason_text = " / ".join(reason_labels) if reason_labels else "重複条件"
    actions = [
        postback_action("今回を削除", f"a=dup_del&r={receipt_id}", "今回を削除"),
        postback_action("このまま登録", f"a=dup_keep&r={receipt_id}", "このまま登録"),
    ]
    text = f"重複の可能性があります（{count}件: {reason_text}）。\n不要なら「今回を削除」を選択してください。"
    return [with_quick_reply(text, actions)]


def build_duplicate_deleted_message() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "重複候補の登録を削除しました。"}]


def build_duplicate_kept_message() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "重複警告をスキップして登録を継続しました。"}]


def build_duplicate_image_skipped_message() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "同一画像の可能性が高いため、今回の送信は処理をスキップしました。"}]


def build_ocr_quota_exceeded_message(reason: str) -> list[dict[str, Any]]:
    normalized = str(reason or "").strip().lower()
    if normalized == "user_minute":
        return [{"type": "text", "text": "短時間に送信が集中しています。1分ほど待ってから再送してください。"}]
    if normalized == "user_day":
        return [{"type": "text", "text": "本日の処理上限に達しました。明日以降に再送してください。"}]
    if normalized == "global_day":
        return [{"type": "text", "text": "本日の受付上限に達したため、一時的に新規処理を停止しています。"}]
    return [{"type": "text", "text": "現在処理を受け付けできません。時間をおいて再送してください。"}]


def build_last_registration_cancelled_message() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "直前の登録を取り消しました。"}]


def build_last_registration_not_found_message() -> list[dict[str, Any]]:
    return [{"type": "text", "text": "取り消せる直前の登録がありません。"}]


def build_non_deductible_warning_message(keywords: list[str]) -> list[dict[str, Any]]:
    seen: list[str] = []
    for keyword in keywords:
        if keyword in seen:
            continue
        seen.append(keyword)
    head = "、".join(seen[:5])
    text = (
        "医療費控除対象外の可能性がある語句を検出しました。\n"
        f"検出語句: {head}\n"
        "最終的な控除可否は領収書内容と税務要件を確認してください。"
    )
    return [{"type": "text", "text": text}]


def is_family_registration_finish_text(text: str) -> bool:
    normalized = str(text or "").strip()
    return normalized in {FAMILY_REGISTRATION_FINISH_TEXT, FAMILY_REGISTRATION_FINISH_TEXT_LEGACY}


def _family_registration_finish_actions(can_finish: bool) -> list[dict[str, Any]]:
    if not can_finish:
        return []
    return [message_action(FAMILY_REGISTRATION_FINISH_TEXT, FAMILY_REGISTRATION_FINISH_TEXT)]


def _family_registration_continue_actions(can_finish: bool) -> list[dict[str, Any]]:
    actions = [message_action(FAMILY_REGISTRATION_NEXT_TEXT, FAMILY_REGISTRATION_NEXT_TEXT)]
    if can_finish:
        actions.append(message_action(FAMILY_REGISTRATION_FINISH_TEXT, FAMILY_REGISTRATION_FINISH_TEXT))
    return actions


def _family_registration_alias_actions(can_finish: bool) -> list[dict[str, Any]]:
    actions = [message_action(FAMILY_REGISTRATION_SKIP_TEXT, FAMILY_REGISTRATION_SKIP_TEXT)]
    if can_finish:
        actions.append(message_action(FAMILY_REGISTRATION_FINISH_TEXT, FAMILY_REGISTRATION_FINISH_TEXT))
    return actions


def build_family_registration_prompt_message(can_finish: bool = True) -> list[dict[str, Any]]:
    text = (
        "家族の名前を1名ずつ登録します。\n"
        "まず、登録したい方の氏名を入力してください。\n"
        "姓と名の間にはスペースを入れてください（全角/半角どちらでも可）。\n"
        "例: 山田 太郎"
    )
    return [with_quick_reply(text, _family_registration_finish_actions(can_finish))]


def build_family_registration_yomi_prompt_message(
    canonical_name: str,
    can_finish: bool = True,
) -> list[dict[str, Any]]:
    text = (
        f"{canonical_name}さんのヨミガナを入力してください。\n"
        "姓と名の間にはスペースを入れてください（全角/半角どちらでも可）。\n"
        "例: ヤマダ タロウ"
    )
    return [with_quick_reply(text, _family_registration_finish_actions(can_finish))]


def build_family_registration_alias_prompt_message(
    canonical_name: str,
    can_finish: bool = True,
) -> list[dict[str, Any]]:
    text = (
        f"{canonical_name}さんで、よく間違えられる表記があれば1つ入力してください。\n"
        f"なければ「{FAMILY_REGISTRATION_SKIP_TEXT}」を押してください。"
    )
    return [with_quick_reply(text, _family_registration_alias_actions(can_finish))]


def build_family_registration_saved_message(
    total_members: int,
    latest: list[str],
    can_finish: bool = True,
) -> list[dict[str, Any]]:
    preview = "、".join(latest[:3])
    head = f"{preview} を登録しました。\n" if preview else ""
    text = (
        f"{head}登録済み: {total_members}名\n"
        f"続ける場合は「{FAMILY_REGISTRATION_NEXT_TEXT}」、"
        f"終了する場合は「{FAMILY_REGISTRATION_FINISH_TEXT}」を押してください。"
    )
    return [with_quick_reply(text, _family_registration_continue_actions(can_finish))]


def build_family_registration_need_member_message() -> list[dict[str, Any]]:
    text = (
        "家族氏名が未登録です。最低1名は登録してください。\n"
        "まずは1名分の氏名を入力してください。"
    )
    return [with_quick_reply(text, _family_registration_finish_actions(False))]


def build_family_registration_need_space_message(
    invalid_names: list[str],
    target_label: str = "名前",
    can_finish: bool = True,
) -> list[dict[str, Any]]:
    preview = "、".join(invalid_names[:3])
    head = f"入力を確認してください: {preview}\n" if preview else ""
    example = "ヤマダ タロウ" if "ヨミ" in target_label else "山田 太郎"
    text = (
        f"{head}{target_label}は姓と名の間にスペースを入れて再入力してください。\n"
        "全角/半角スペースはどちらでも入力できます（内部では半角スペースに正規化します）。\n"
        f"例: {example}"
    )
    return [with_quick_reply(text, _family_registration_finish_actions(can_finish))]


def build_family_registration_completed_message(total_members: int) -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": f"家族氏名の登録が完了しました（{total_members}名）。領収書画像を送信してください。",
        }
    ]


def build_photo_registration_start_message(total_members: int) -> list[dict[str, Any]]:
    text = (
        f"家族氏名の登録が完了しました（{total_members}名）。\n"
        "このあと領収書画像を送信してください。\n"
        "撮影のポイント（こうすると文字の認識がうまくいきます）:\n"
        "- なるべく真上から撮影する\n"
        "- 複数の領収書を並べたり重ねたりしない\n"
        "- なるべく明るい場所で撮影する\n"
        "- 領収書が白飛びしないようにする\n"
        "- なるべく画角いっぱいに領収書を収める"
    )
    return [{"type": "text", "text": text}]

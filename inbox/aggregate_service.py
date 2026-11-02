from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from inbox.repository_interface import InboxRepositoryProtocol
from ledger.service import LedgerService
from linebot import message_templates

JST = timezone(timedelta(hours=9))
TAX_FORM_COMMANDS = {"申告資料", "申告", "確定申告", "医療費控除"}
LEDGER_COMMANDS = {"明細", "明細一覧", "明細を見る"}


class AggregateService:
    def __init__(self, repository: InboxRepositoryProtocol, liff_id: str | None = None) -> None:
        self.repository = repository
        self.liff_id = str(liff_id or "").strip()

    def liff_url(self, **params: Any) -> str | None:
        if not self.liff_id:
            return None
        query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
        return f"https://liff.line.me/{self.liff_id}" + (f"?{query}" if query else "")

    def handle_text_command(self, line_user_id: str, text: str) -> list[dict[str, Any]] | None:
        command = (text or "").strip()
        now = datetime.now(timezone.utc)
        year = now.year
        month = now.month
        pending = self.repository.get_pending_count(line_user_id)

        if command == "今年の医療費":
            total, count = self.repository.get_year_summary(line_user_id, year)
            return message_templates.build_aggregate_message(f"{year}年の医療費", total, count, pending)
        if command == "今月の医療費":
            total, count = self.repository.get_month_summary(line_user_id, year, month)
            return message_templates.build_aggregate_message(f"{year}年{month}月の医療費", total, count, pending)
        if command == "未確認":
            return [
                {
                    "type": "text",
                    "text": f"未確認件数: {pending}件\n必要なら領収書画像を再送してください。",
                }
            ]
        if command in TAX_FORM_COMMANDS:
            return self._tax_form_message(line_user_id)
        if command in LEDGER_COMMANDS:
            url = self.liff_url()
            if url is None:
                return message_templates.build_ledger_screen_unavailable_message()
            return message_templates.build_ledger_link_message(url)
        if command == "ヘルプ":
            return message_templates.build_help_message()
        return None

    def _tax_form_message(self, line_user_id: str) -> list[dict[str, Any]]:
        # 1〜3月は確定申告の時期なので前年分を案内する
        now = datetime.now(JST)
        year = now.year - 1 if now.month <= 3 else now.year
        url = self.liff_url(view="export", year=year)
        if url is None:
            return message_templates.build_ledger_screen_unavailable_message()
        export = LedgerService(self.repository).prepare_tax_form(line_user_id, year)
        return message_templates.build_tax_form_message(
            year=year,
            included_count=export.included_count,
            net_amount_yen=export.total_amount_yen - export.total_reimbursed_yen,
            excluded_count=len(export.excluded),
            url=url,
        )

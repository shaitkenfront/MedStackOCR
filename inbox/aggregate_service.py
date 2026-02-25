from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from inbox.repository import InboxRepository
from linebot import message_templates


class AggregateService:
    def __init__(self, repository: InboxRepository) -> None:
        self.repository = repository

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
        if command == "ヘルプ":
            return message_templates.build_help_message()
        return None


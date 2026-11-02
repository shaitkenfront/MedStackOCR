from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ConversationSession:
    session_id: str
    line_user_id: str
    receipt_id: str
    state: str
    awaiting_field: str | None
    payload: dict[str, Any]
    expires_at: str
    created_at: str
    updated_at: str


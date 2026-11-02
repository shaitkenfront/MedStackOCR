from __future__ import annotations

import unittest
from typing import Any

from linebot.reply_client import LineReplyClient


class CapturingReplyClient(LineReplyClient):
    def __init__(self) -> None:
        super().__init__(channel_access_token="token")
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def _post_json(self, path: str, payload: dict[str, Any]) -> None:
        self.posts.append((path, payload))


class LineReplyClientTest(unittest.TestCase):
    def test_reply_keeps_quick_reply_only_on_last_message(self) -> None:
        client = CapturingReplyClient()
        first_quick_reply = {"items": [{"type": "action", "action": {"type": "message", "label": "修正", "text": "修正"}}]}
        candidate_quick_reply = {
            "items": [
                {
                    "type": "action",
                    "action": {"type": "postback", "label": "自分で入力する", "data": "a=free_text"},
                }
            ]
        }

        client.reply(
            "reply-token",
            [
                {"type": "text", "text": "確認", "quickReply": first_quick_reply},
                {"type": "text", "text": "説明"},
                {"type": "text", "text": "候補", "quickReply": candidate_quick_reply},
            ],
        )

        _, payload = client.posts[0]
        sent_messages = payload["messages"]
        self.assertNotIn("quickReply", sent_messages[0])
        self.assertNotIn("quickReply", sent_messages[1])
        self.assertEqual(sent_messages[2].get("quickReply"), candidate_quick_reply)

    def test_reply_moves_last_quick_reply_to_terminal_message(self) -> None:
        client = CapturingReplyClient()
        quick_reply = {"items": [{"type": "action", "action": {"type": "postback", "label": "候補1", "data": "a=pick"}}]}

        client.reply(
            "reply-token",
            [
                {"type": "text", "text": "候補", "quickReply": quick_reply},
                {"type": "text", "text": "補足"},
            ],
        )

        _, payload = client.posts[0]
        sent_messages = payload["messages"]
        self.assertNotIn("quickReply", sent_messages[0])
        self.assertEqual(sent_messages[1].get("quickReply"), quick_reply)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from linebot.event_ids import build_line_event_id


class LineEventIdsTest(unittest.TestCase):
    def test_prioritize_webhook_event_id(self) -> None:
        event = {"webhookEventId": "evt-123", "timestamp": 1, "type": "message", "message": {"id": "m1"}}
        self.assertEqual(build_line_event_id(event), "evt-123")

    def test_fallback_to_timestamp_type_message_id(self) -> None:
        event = {"timestamp": 1700000000, "type": "message", "message": {"id": "m1"}}
        self.assertEqual(build_line_event_id(event), "1700000000:message:m1")


if __name__ == "__main__":
    unittest.main()

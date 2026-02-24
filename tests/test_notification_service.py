from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from io_utils.json_writer import write_json
from notifications.service import NotificationService


class _DummyNotifier:
    def __init__(self, should_fail: bool = False) -> None:
        self.messages: list[str] = []
        self.should_fail = should_fail

    def send(self, message: str) -> None:
        if self.should_fail:
            raise RuntimeError("send failed")
        self.messages.append(message)


def _builder_with_notifiers(notifiers: dict[str, _DummyNotifier], errors: dict[str, str] | None = None) -> Any:
    def _build(_: dict[str, Any]) -> tuple[dict[str, _DummyNotifier], dict[str, str]]:
        return notifiers, dict(errors or {})

    return _build


class NotificationServiceTest(unittest.TestCase):
    def test_skip_when_disabled(self) -> None:
        notifier = _DummyNotifier()
        service = NotificationService(
            {"notifications": {"enabled": False}},
            channel_builder=_builder_with_notifiers({"slack": notifier}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = service.notify_new_receipts(Path(tmp), [Path(tmp) / "a.jpg"])
        self.assertTrue(result.skipped)
        self.assertEqual(notifier.messages, [])

    def test_notify_new_receipts_with_limit_and_error_collection(self) -> None:
        slack = _DummyNotifier()
        discord = _DummyNotifier(should_fail=True)
        service = NotificationService(
            {
                "notifications": {
                    "enabled": True,
                    "max_items_in_message": 2,
                }
            },
            channel_builder=_builder_with_notifiers({"slack": slack, "discord": discord}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            images = [base / "c.jpg", base / "a.jpg", base / "b.jpg"]
            for image, amount in ((images[0], 100), (images[1], 200), (images[2], 300)):
                write_json(
                    base / f"{image.stem}.result.json",
                    {
                        "fields": {
                            "payment_date": {"value_normalized": "2026-02-23"},
                            "family_member_name": {"value_normalized": "山田 太郎"},
                            "payer_facility_name": {"value_normalized": f"クリニック{image.stem}"},
                            "payment_amount": {"value_normalized": amount},
                        }
                    },
                    pretty=True,
                )
            result = service.notify_new_receipts(base, images)

        self.assertFalse(result.skipped)
        self.assertEqual(result.sent_channels, ["slack"])
        self.assertIn("discord", result.failed_channels)
        self.assertEqual(len(slack.messages), 1)
        sent = slack.messages[0]
        self.assertIn("現時点での医療費合計: 600", sent)
        self.assertIn("件数: 3", sent)
        self.assertIn("- date, patient_name, clinic_or_pharmacy_name, amount", sent)
        self.assertIn("- 2026-02-23, 山田 太郎, クリニックa, 200", sent)
        self.assertIn("- 2026-02-23, 山田 太郎, クリニックb, 300", sent)
        self.assertIn("... 他 1 件", sent)

    def test_skip_when_no_new_images(self) -> None:
        notifier = _DummyNotifier()
        service = NotificationService(
            {"notifications": {"enabled": True}},
            channel_builder=_builder_with_notifiers({"slack": notifier}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = service.notify_new_receipts(Path(tmp), [])
        self.assertTrue(result.skipped)
        self.assertEqual(notifier.messages, [])


if __name__ == "__main__":
    unittest.main()

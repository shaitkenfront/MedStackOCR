from __future__ import annotations

import unittest

from notifications.factory import build_notification_channels


class NotificationFactoryTest(unittest.TestCase):
    def test_build_channels_with_valid_settings(self) -> None:
        config = {
            "notifications": {
                "channels": ["slack", "discord"],
                "slack": {"webhook_url": "https://example.com/slack"},
                "discord": {"webhook_url": "https://example.com/discord"},
            }
        }
        channels, errors = build_notification_channels(config)
        self.assertEqual(sorted(channels.keys()), ["discord", "slack"])
        self.assertEqual(errors, {})

    def test_build_channels_returns_errors_for_missing_or_unknown(self) -> None:
        config = {
            "notifications": {
                "channels": ["line", "unknown"],
                "line": {"channel_access_token": "", "to": ""},
            }
        }
        channels, errors = build_notification_channels(config)
        self.assertEqual(channels, {})
        self.assertIn("line", errors)
        self.assertIn("unknown", errors)


if __name__ == "__main__":
    unittest.main()

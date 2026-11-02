from __future__ import annotations

import unittest
from unittest import mock

from inbox.repository import InboxRepository
from inbox.repository_factory import create_inbox_repository


class RepositoryFactoryTest(unittest.TestCase):
    def test_create_sqlite_repository_by_default(self) -> None:
        config = {"inbox": {"sqlite_path": "data/inbox/linebot.db"}}
        repository = create_inbox_repository(config)
        self.assertIsInstance(repository, InboxRepository)

    def test_create_dynamodb_repository(self) -> None:
        config = {
            "inbox": {
                "backend": "dynamodb",
                "dynamodb": {
                    "region": "ap-northeast-1",
                    "table_prefix": "medstackocr",
                    "event_ttl_days": 7,
                    "tables": {
                        "event_dedupe": "evt",
                        "receipts": "rec",
                        "receipt_fields": "fld",
                        "sessions": "ses",
                        "aggregate_entries": "agg",
                        "family_registry": "fam",
                        "learning_rules": "learn",
                        "ocr_usage_guard": "guard",
                    },
                },
            }
        }
        with mock.patch("inbox.repository_factory.DynamoInboxRepository") as constructor:
            _ = create_inbox_repository(config)
        constructor.assert_called_once()


if __name__ == "__main__":
    unittest.main()

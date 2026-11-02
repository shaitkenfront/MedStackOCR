from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from core.enums import FieldName
from inbox.dynamo_repository import ClientError, DynamoInboxRepository


def _validation_add_operand_error(operation: str) -> Exception:
    message = (
        "Invalid UpdateExpression: Incorrect operand type for operator or function; "
        "operator: ADD, operand type: MAP, typeSet: ALLOWED_FOR_ADD_OPERAND"
    )
    response = {"Error": {"Code": "ValidationException", "Message": message}}
    if ClientError is Exception:
        error = Exception(message)
        setattr(error, "response", response)
        return error
    return ClientError(response, operation)


def _build_repo_for_test() -> DynamoInboxRepository:
    repo = DynamoInboxRepository.__new__(DynamoInboxRepository)
    repo._learning_table = mock.Mock()
    repo._usage_guard_table = mock.Mock()
    repo._usage_guard_table.name = "test-ocr-usage-guard"
    repo._ddb = mock.Mock()
    repo._ddb.meta.client = mock.Mock()
    repo._to_ddb_key = lambda value: value  # type: ignore[assignment]
    return repo


class DynamoInboxRepositoryTest(unittest.TestCase):
    def test_consume_ocr_quota_uses_native_values_for_transact_write(self) -> None:
        repo = _build_repo_for_test()
        repo._ddb.meta.client.transact_write_items.return_value = {}

        allowed, reason = repo.consume_ocr_quota(
            line_user_id="U1",
            now_utc=datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc),
            user_per_minute_limit=3,
            user_per_day_limit=10,
            global_per_day_limit=20,
        )

        self.assertTrue(allowed)
        self.assertIsNone(reason)
        call = repo._ddb.meta.client.transact_write_items.call_args
        transact_items = call.kwargs["TransactItems"]
        first_update = transact_items[0]["Update"]
        self.assertEqual(first_update["Key"]["scope_key"], "USER#U1")
        self.assertEqual(first_update["ExpressionAttributeValues"][":incr"], 1)

    def test_record_field_correction_recovers_from_add_operand_map(self) -> None:
        repo = _build_repo_for_test()
        repo._learning_table.update_item.side_effect = [
            _validation_add_operand_error("UpdateItem"),
            None,
            None,
        ]
        repo._learning_table.get_item.return_value = {"Item": {"count": {"N": "4"}}}

        repo.record_field_correction("U1", FieldName.FAMILY_MEMBER_NAME, "テスト医院", "山田 花子")

        self.assertEqual(repo._learning_table.update_item.call_count, 3)
        normalize_call = repo._learning_table.update_item.call_args_list[1]
        self.assertEqual(normalize_call.kwargs["ExpressionAttributeValues"][":count"], 4)

    def test_consume_ocr_quota_recovers_from_add_operand_map(self) -> None:
        repo = _build_repo_for_test()
        repo._ddb.meta.client.transact_write_items.side_effect = [
            _validation_add_operand_error("TransactWriteItems"),
            None,
        ]
        repo._usage_guard_table.get_item.return_value = {"Item": {"count": {"N": "2"}}}

        allowed, reason = repo.consume_ocr_quota(
            line_user_id="U1",
            now_utc=datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc),
            user_per_minute_limit=3,
            user_per_day_limit=10,
            global_per_day_limit=20,
        )

        self.assertTrue(allowed)
        self.assertIsNone(reason)
        self.assertEqual(repo._ddb.meta.client.transact_write_items.call_count, 2)
        self.assertEqual(repo._usage_guard_table.update_item.call_count, 3)
        for call in repo._usage_guard_table.update_item.call_args_list:
            self.assertEqual(call.kwargs["ExpressionAttributeValues"][":count"], 2)


if __name__ == "__main__":
    unittest.main()

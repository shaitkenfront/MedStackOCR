from __future__ import annotations

import unittest
from unittest import mock

from app.lambda_handlers.api_handler import build_api_from_env


class ApiHandlerTest(unittest.TestCase):
    def test_image_deleter_only_deletes_inside_receipt_bucket(self) -> None:
        s3 = mock.Mock()
        api = build_api_from_env(
            {"RECEIPT_BUCKET": "receipts", "LINE_LOGIN_CHANNEL_ID": "1"},
            dynamodb_resource=mock.Mock(),
            s3_client=s3,
        )
        assert api.image_deleter is not None
        api.image_deleter("s3://receipts/raw/2026/R1.jpg")
        api.image_deleter("s3://other-bucket/R2.jpg")
        api.image_deleter("/tmp/local.jpg")
        s3.delete_object.assert_called_once_with(Bucket="receipts", Key="raw/2026/R1.jpg")

    def test_channel_id_comes_from_env(self) -> None:
        api = build_api_from_env({"LINE_LOGIN_CHANNEL_ID": " 1650 "}, dynamodb_resource=mock.Mock(), s3_client=mock.Mock())
        self.assertEqual(api.verifier.channel_id, "1650")


if __name__ == "__main__":
    unittest.main()

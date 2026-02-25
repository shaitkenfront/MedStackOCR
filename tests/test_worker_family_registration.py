from __future__ import annotations

import unittest

from app.lambda_handlers.worker_handler import _parse_family_registration_entries, _parse_s3_uri


class WorkerFamilyRegistrationTest(unittest.TestCase):
    def test_parse_family_registration_entries(self) -> None:
        text = "山田 太郎, ヤマダ タロウ, 山田太朗\n山田 花子"
        entries = _parse_family_registration_entries(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0][0], "山田 太郎")
        self.assertEqual(entries[0][1], ["ヤマダ タロウ", "山田太朗"])
        self.assertEqual(entries[1][0], "山田 花子")
        self.assertEqual(entries[1][1], [])

    def test_parse_s3_uri(self) -> None:
        bucket, key = _parse_s3_uri("s3://my-bucket/raw/2026/02/file.jpg")
        self.assertEqual(bucket, "my-bucket")
        self.assertEqual(key, "raw/2026/02/file.jpg")


if __name__ == "__main__":
    unittest.main()

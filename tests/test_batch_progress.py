from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from io_utils.batch_progress import (
    is_already_processed,
    load_processed_registry,
    save_processed_registry,
    update_processed_registry,
    write_summary_csv,
)
from io_utils.json_writer import write_json


class BatchProgressTest(unittest.TestCase):
    def test_processed_registry_roundtrip_and_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            registry_path = base / "processed_files.json"
            image_path = base / "sample.jpg"
            image_path.write_bytes(b"abc")

            registry = load_processed_registry(registry_path)
            self.assertEqual(registry, {})
            self.assertFalse(is_already_processed(registry, image_path))

            update_processed_registry(registry, image_path)
            save_processed_registry(registry_path, registry)

            reloaded = load_processed_registry(registry_path)
            self.assertTrue(is_already_processed(reloaded, image_path))

            image_path.write_bytes(b"abcd")
            self.assertFalse(is_already_processed(reloaded, image_path))

    def test_write_summary_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            write_json(
                base / "clinic_receipt_001.result.json",
                {
                    "fields": {
                        "payment_date": {"value_normalized": "2026-01-27"},
                        "family_member_name": {"value_normalized": "山田 太郎"},
                        "payer_facility_name": {"value_normalized": "サンプルクリニック 本院"},
                        "payment_amount": {"value_normalized": 2640},
                    }
                },
                pretty=True,
            )
            write_json(
                base / "pharmacy_receipt_001.result.json",
                {
                    "fields": {
                        "payment_date": {"value_normalized": "2026-01-27"},
                        "family_member_name": {"value_normalized": "山田 花子"},
                        "payer_facility_name": None,
                        "prescribing_facility_name": {"value_normalized": "サンプル薬局"},
                        "payment_amount": {"value_normalized": 1800},
                    }
                },
                pretty=True,
            )

            csv_path = write_summary_csv(base)
            self.assertTrue(csv_path.exists())

            with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
                rows = list(csv.reader(fp))

            self.assertEqual(rows[0], ["日付", "氏名", "医療機関・調剤薬局名", "金額"])
            self.assertEqual(rows[1], ["2026-01-27", "山田 太郎", "サンプルクリニック 本院", "2640"])
            self.assertEqual(rows[2], ["2026-01-27", "山田 花子", "サンプル薬局", "1800"])


if __name__ == "__main__":
    unittest.main()

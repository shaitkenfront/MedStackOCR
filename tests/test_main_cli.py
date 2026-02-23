from __future__ import annotations

import unittest
from copy import deepcopy

from app.config import DEFAULT_CONFIG
from app.main import _apply_force_cpu_config, build_parser


class MainCliTest(unittest.TestCase):
    def test_extract_command_accepts_force_cpu(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "extract",
                "--image",
                "data/samples/clinic_receipt_001.jpg",
                "--household-id",
                "household_demo",
                "--force-cpu",
                "--output",
                "data/outputs/clinic_receipt_001.result.json",
            ]
        )
        self.assertEqual(args.command, "extract")
        self.assertTrue(args.force_cpu)

    def test_refresh_summary_command_accepts_target_dir(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["refresh-summary", "--target-dir", "data/outputs/yomitoku_tuned"])
        self.assertEqual(args.command, "refresh-summary")
        self.assertEqual(args.target_dir, "data/outputs/yomitoku_tuned")

    def test_batch_command_accepts_target_dir(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "batch",
                "--household-id",
                "household_demo",
                "--force-cpu",
                "--target-dir",
                "data/outputs/yomitoku_tuned",
            ]
        )
        self.assertEqual(args.command, "batch")
        self.assertEqual(args.target_dir, "data/outputs/yomitoku_tuned")
        self.assertTrue(args.force_cpu)

    def test_compare_command_accepts_target_dir(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "compare-ocr",
                "--image",
                "data/samples/clinic_receipt_001.jpg",
                "--household-id",
                "household_demo",
                "--ocr-engines",
                "yomitoku",
                "--force-cpu",
                "--target-dir",
                "data/outputs/compare",
            ]
        )
        self.assertEqual(args.command, "compare-ocr")
        self.assertEqual(args.target_dir, "data/outputs/compare")
        self.assertTrue(args.force_cpu)

    def test_healthcheck_command_accepts_force_cpu(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["healthcheck-ocr", "--ocr-engines", "yomitoku", "--force-cpu"])
        self.assertEqual(args.command, "healthcheck-ocr")
        self.assertTrue(args.force_cpu)

    def test_force_cpu_override_sets_yomitoku_device(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        updated = _apply_force_cpu_config(config, force_cpu=True, target_engines=["yomitoku"])
        self.assertEqual(updated["ocr"]["engines"]["yomitoku"]["device"], "cpu")
        self.assertEqual(config["ocr"]["engines"]["yomitoku"]["device"], "cuda")

    def test_force_cpu_override_skips_non_yomitoku_engine(self) -> None:
        config = deepcopy(DEFAULT_CONFIG)
        updated = _apply_force_cpu_config(config, force_cpu=True, target_engines=["tesseract"])
        self.assertIs(updated, config)

    def test_batch_command_rejects_legacy_output_dir(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "batch",
                    "--household-id",
                    "household_demo",
                    "--output-dir",
                    "data/outputs/yomitoku_tuned",
                ]
            )

    def test_batch_command_rejects_legacy_input_dir(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "batch",
                    "--household-id",
                    "household_demo",
                    "--target-dir",
                    "data/outputs/yomitoku_tuned",
                    "--input-dir",
                    "data/samples",
                ]
            )

    def test_compare_command_rejects_legacy_output_dir(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "compare-ocr",
                    "--image",
                    "data/samples/clinic_receipt_001.jpg",
                    "--household-id",
                    "household_demo",
                    "--ocr-engines",
                    "yomitoku",
                    "--output-dir",
                    "data/outputs/compare",
                ]
            )


if __name__ == "__main__":
    unittest.main()

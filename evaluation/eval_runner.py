from __future__ import annotations

import argparse
from pathlib import Path

from evaluation.metrics import EvalMetrics
from io_utils.json_writer import load_json, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate extraction results")
    parser.add_argument("--pred-dir", required=True, help="Directory that contains prediction JSON files")
    parser.add_argument("--gt-dir", required=True, help="Directory that contains ground truth JSON files")
    parser.add_argument("--output", default=None, help="Output path for aggregated metrics JSON")
    return parser


def run(pred_dir: str, gt_dir: str) -> EvalMetrics:
    pred_base = Path(pred_dir)
    gt_base = Path(gt_dir)
    metrics = EvalMetrics()

    gt_files = sorted(gt_base.glob("*.json"))
    for gt_file in gt_files:
        pred_file = pred_base / gt_file.name
        if not pred_file.exists():
            continue
        gt = load_json(gt_file)
        pred = load_json(pred_file)
        metrics.add(predicted=pred, ground_truth=gt)
    return metrics


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    metrics = run(args.pred_dir, args.gt_dir)
    payload = metrics.to_dict()

    if args.output:
        output_path = write_json(args.output, payload, pretty=True)
        print(f"saved metrics: {output_path}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from app.config import load_config
from app.pipeline import ReceiptExtractionPipeline
from io_utils.image_loader import list_images
from io_utils.json_writer import load_json, write_json
from ocr.factory import create_ocr_adapter
from templates.learner import TemplateLearner
from templates.store import TemplateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Medical receipt extractor MVP")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract", help="Extract one receipt image")
    extract_parser.add_argument("--config", default=None, help="Path to config.yaml or config.json")
    extract_parser.add_argument("--image", required=True)
    extract_parser.add_argument("--household-id", required=True)
    extract_parser.add_argument("--ocr-engine", default=None)
    extract_parser.add_argument("--output", required=True)

    batch_parser = subparsers.add_parser("batch", help="Extract all images in a directory")
    batch_parser.add_argument("--config", default=None, help="Path to config.yaml or config.json")
    batch_parser.add_argument("--input-dir", required=True)
    batch_parser.add_argument("--household-id", required=True)
    batch_parser.add_argument("--ocr-engine", default=None)
    batch_parser.add_argument("--output-dir", required=True)

    compare_parser = subparsers.add_parser("compare-ocr", help="Run multiple OCR engines for one image")
    compare_parser.add_argument("--config", default=None, help="Path to config.yaml or config.json")
    compare_parser.add_argument("--image", required=True)
    compare_parser.add_argument("--household-id", required=True)
    compare_parser.add_argument("--ocr-engines", required=True, help="Comma-separated engines")
    compare_parser.add_argument("--output-dir", required=True)

    health_parser = subparsers.add_parser("healthcheck-ocr", help="Check OCR adapter availability")
    health_parser.add_argument("--config", default=None, help="Path to config.yaml or config.json")
    health_parser.add_argument("--ocr-engines", required=True, help="Comma-separated engines")
    health_parser.add_argument("--output", default=None, help="Optional output JSON path")

    learn_parser = subparsers.add_parser("learn-template", help="Learn household template from review correction")
    learn_parser.add_argument("--config", default=None, help="Path to config.yaml or config.json")
    learn_parser.add_argument("--document-result", required=True)
    learn_parser.add_argument("--review-correction", required=True)

    return parser


def cmd_extract(args: argparse.Namespace, config: dict[str, Any]) -> int:
    engine = args.ocr_engine or config.get("ocr", {}).get("engine", "mock")
    pipeline = ReceiptExtractionPipeline(config)
    result = pipeline.process(
        image_path=args.image,
        household_id=args.household_id,
        ocr_engine=engine,
    )
    output_path = write_json(
        args.output,
        payload=result.to_dict(),
        pretty=bool(config.get("output", {}).get("pretty_json", True)),
    )
    print(f"saved: {output_path} status={result.decision.status.value} confidence={result.decision.confidence:.3f}")
    return 0


def cmd_batch(args: argparse.Namespace, config: dict[str, Any]) -> int:
    images = list_images(args.input_dir)
    if not images:
        print(f"no images found: {args.input_dir}")
        return 1

    engine = args.ocr_engine or config.get("ocr", {}).get("engine", "mock")
    pipeline = ReceiptExtractionPipeline(config)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []

    for image in images:
        result = pipeline.process(
            image_path=str(image),
            household_id=args.household_id,
            ocr_engine=engine,
        )
        output_path = output_dir / f"{image.stem}.result.json"
        write_json(
            output_path,
            payload=result.to_dict(),
            pretty=bool(config.get("output", {}).get("pretty_json", True)),
        )
        summary.append(
            {
                "image": str(image),
                "output": str(output_path),
                "status": result.decision.status.value,
                "confidence": round(result.decision.confidence, 4),
            }
        )

    summary_path = write_json(output_dir / "summary.json", {"items": summary}, pretty=True)
    print(f"processed={len(summary)} summary={summary_path}")
    return 0


def cmd_compare_ocr(args: argparse.Namespace, config: dict[str, Any]) -> int:
    engines = [e.strip() for e in args.ocr_engines.split(",") if e.strip()]
    if not engines:
        print("ocr engines are empty")
        return 1

    pipeline = ReceiptExtractionPipeline(config)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []

    for engine in engines:
        try:
            result = pipeline.process(
                image_path=args.image,
                household_id=args.household_id,
                ocr_engine=engine,
            )
            output_path = output_dir / f"{Path(args.image).stem}.{engine}.json"
            write_json(
                output_path,
                payload=result.to_dict(),
                pretty=bool(config.get("output", {}).get("pretty_json", True)),
            )
            summary.append(
                {
                    "engine": engine,
                    "output": str(output_path),
                    "status": result.decision.status.value,
                    "confidence": round(result.decision.confidence, 4),
                    "document_type": result.document_type.value,
                }
            )
        except Exception as exc:  # noqa: BLE001
            summary.append(
                {
                    "engine": engine,
                    "error": str(exc),
                }
            )

    summary_path = write_json(output_dir / "summary.json", {"items": summary}, pretty=True)
    print(f"compare-complete engines={len(engines)} summary={summary_path}")
    return 0


def cmd_learn_template(args: argparse.Namespace, config: dict[str, Any]) -> int:
    document_result = load_json(args.document_result)
    review_fix = load_json(args.review_correction)

    template_root = config.get("templates", {}).get("store_path", "data/templates")
    store = TemplateStore(template_root)
    learner = TemplateLearner(store)
    template, path = learner.learn_from_review(document_result=document_result, review_fix=review_fix)
    print(f"template-saved: {path} family={template.get('template_family_id')}")
    return 0


def cmd_healthcheck_ocr(args: argparse.Namespace, config: dict[str, Any]) -> int:
    engines = [e.strip() for e in args.ocr_engines.split(",") if e.strip()]
    if not engines:
        print("ocr engines are empty")
        return 1

    items: list[dict[str, Any]] = []
    for engine in engines:
        try:
            adapter = create_ocr_adapter(engine, config)
            ok = bool(adapter.healthcheck())
            items.append(
                {
                    "engine": engine,
                    "available": ok,
                    "adapter": type(adapter).__name__,
                }
            )
        except Exception as exc:  # noqa: BLE001
            items.append(
                {
                    "engine": engine,
                    "available": False,
                    "error": str(exc),
                }
            )

    payload = {"items": items}
    if args.output:
        output_path = write_json(args.output, payload, pretty=True)
        print(f"saved: {output_path}")
    else:
        print(payload)

    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config = load_config(args.config)

    if args.command == "extract":
        return cmd_extract(args, config)
    if args.command == "batch":
        return cmd_batch(args, config)
    if args.command == "compare-ocr":
        return cmd_compare_ocr(args, config)
    if args.command == "healthcheck-ocr":
        return cmd_healthcheck_ocr(args, config)
    if args.command == "learn-template":
        return cmd_learn_template(args, config)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

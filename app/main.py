from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.config import load_config
from app.pipeline import ReceiptExtractionPipeline
from io_utils.batch_progress import (
    is_already_processed,
    load_processed_registry,
    save_processed_registry,
    update_processed_registry,
    write_summary_csv,
)
from io_utils.image_loader import list_images
from io_utils.json_writer import load_json, write_json
from ocr.factory import create_ocr_adapter
from resolver.year_consistency import apply_year_consistency
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
    extract_parser.add_argument("--force-cpu", action="store_true")
    extract_parser.add_argument("--output", required=True)

    batch_parser = subparsers.add_parser("batch", help="Extract all images in a directory")
    batch_parser.add_argument("--config", default=None, help="Path to config.yaml or config.json")
    batch_parser.add_argument("--household-id", required=True)
    batch_parser.add_argument("--ocr-engine", default=None)
    batch_parser.add_argument("--force-cpu", action="store_true")
    batch_parser.add_argument("--target-dir", required=True)

    compare_parser = subparsers.add_parser("compare-ocr", help="Run multiple OCR engines for one image")
    compare_parser.add_argument("--config", default=None, help="Path to config.yaml or config.json")
    compare_parser.add_argument("--image", required=True)
    compare_parser.add_argument("--household-id", required=True)
    compare_parser.add_argument("--ocr-engines", required=True, help="Comma-separated engines")
    compare_parser.add_argument("--force-cpu", action="store_true")
    compare_parser.add_argument("--target-dir", required=True)

    health_parser = subparsers.add_parser("healthcheck-ocr", help="Check OCR adapter availability")
    health_parser.add_argument("--config", default=None, help="Path to config.yaml or config.json")
    health_parser.add_argument("--ocr-engines", required=True, help="Comma-separated engines")
    health_parser.add_argument("--force-cpu", action="store_true")
    health_parser.add_argument("--output", default=None, help="Optional output JSON path")

    learn_parser = subparsers.add_parser("learn-template", help="Learn household template from review correction")
    learn_parser.add_argument("--config", default=None, help="Path to config.yaml or config.json")
    learn_parser.add_argument("--document-result", required=True)
    learn_parser.add_argument("--review-correction", required=True)

    summary_parser = subparsers.add_parser("refresh-summary", help="Regenerate summary.csv in target folder")
    summary_parser.add_argument("--config", default=None, help="Path to config.yaml or config.json")
    summary_parser.add_argument("--target-dir", required=True)

    return parser


def _canonical_engine_name(name: str) -> str:
    lowered = name.strip().lower()
    if lowered in {"deepseek-ocr", "deepseek_ocr"}:
        return "deepseek"
    return lowered


def _apply_force_cpu_config(
    config: dict[str, Any],
    force_cpu: bool,
    target_engines: list[str] | None = None,
) -> dict[str, Any]:
    if not force_cpu:
        return config
    if target_engines is not None:
        normalized = {_canonical_engine_name(str(engine)) for engine in target_engines if str(engine).strip()}
        if "yomitoku" not in normalized:
            return config

    updated = deepcopy(config)
    ocr_conf = updated.setdefault("ocr", {})
    engines_conf = ocr_conf.setdefault("engines", {})
    yomitoku_conf = engines_conf.setdefault("yomitoku", {})
    yomitoku_conf["device"] = "cpu"
    return updated


def cmd_extract(args: argparse.Namespace, config: dict[str, Any]) -> int:
    engine = args.ocr_engine or config.get("ocr", {}).get("engine", "yomitoku")
    runtime_config = _apply_force_cpu_config(config, force_cpu=bool(args.force_cpu), target_engines=[engine])
    try:
        pipeline = ReceiptExtractionPipeline(runtime_config)
        result = pipeline.process(
            image_path=args.image,
            household_id=args.household_id,
            ocr_engine=engine,
        )
        apply_year_consistency([result], runtime_config)
    except Exception as exc:  # noqa: BLE001
        print(f"extract failed: {exc}")
        return 1
    output_path = write_json(
        args.output,
        payload=result.to_dict(),
        pretty=bool(runtime_config.get("output", {}).get("pretty_json", True)),
    )
    print(f"saved: {output_path} status={result.decision.status.value} confidence={result.decision.confidence:.3f}")
    return 0


def cmd_batch(args: argparse.Namespace, config: dict[str, Any]) -> int:
    target_dir = Path(args.target_dir)
    images = list_images(str(target_dir))
    if not images:
        print(f"no images found: {target_dir}")
        return 1

    engine = args.ocr_engine or config.get("ocr", {}).get("engine", "yomitoku")
    runtime_config = _apply_force_cpu_config(config, force_cpu=bool(args.force_cpu), target_engines=[engine])
    try:
        pipeline = ReceiptExtractionPipeline(runtime_config)
    except Exception as exc:  # noqa: BLE001
        print(f"batch failed: {exc}")
        return 1
    output_dir = target_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    processed_registry_path = output_dir / "processed_files.json"
    processed_registry = load_processed_registry(processed_registry_path)

    summary: list[dict[str, Any]] = []
    failed = 0
    skipped = 0
    succeeded: list[tuple[Path, Any]] = []
    unprocessed_images: list[Path] = []

    for image in images:
        if is_already_processed(processed_registry, image):
            skipped += 1
            summary.append(
                {
                    "image": str(image),
                    "status": "SKIPPED_ALREADY_PROCESSED",
                }
            )
            continue
        unprocessed_images.append(image)

    for image in unprocessed_images:
        try:
            result = pipeline.process(
                image_path=str(image),
                household_id=args.household_id,
                ocr_engine=engine,
            )
            succeeded.append((image, result))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            summary.append(
                {
                    "image": str(image),
                    "status": "FAILED",
                    "error": str(exc),
                }
            )

    if succeeded:
        apply_year_consistency([result for _, result in succeeded], runtime_config)
        for image, result in succeeded:
            output_path = output_dir / f"{image.stem}.result.json"
            write_json(
                output_path,
                payload=result.to_dict(),
                pretty=bool(runtime_config.get("output", {}).get("pretty_json", True)),
            )
            update_processed_registry(processed_registry, image)
            summary.append(
                {
                    "image": str(image),
                    "output": str(output_path),
                    "status": result.decision.status.value,
                    "confidence": round(result.decision.confidence, 4),
                }
            )

    registry_path = save_processed_registry(processed_registry_path, processed_registry)
    csv_path = write_summary_csv(output_dir)
    summary_path = write_json(output_dir / "summary.json", {"items": summary}, pretty=True)
    print(
        f"processed={len(succeeded)} skipped={skipped} failed={failed} "
        f"summary={summary_path} csv={csv_path} registry={registry_path}"
    )
    return 0 if failed == 0 else 1


def cmd_compare_ocr(args: argparse.Namespace, config: dict[str, Any]) -> int:
    engines = [e.strip() for e in args.ocr_engines.split(",") if e.strip()]
    if not engines:
        print("ocr engines are empty")
        return 1

    runtime_config = _apply_force_cpu_config(config, force_cpu=bool(args.force_cpu), target_engines=engines)
    try:
        pipeline = ReceiptExtractionPipeline(runtime_config)
    except Exception as exc:  # noqa: BLE001
        print(f"compare failed: {exc}")
        return 1
    output_dir = Path(args.target_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []

    for engine in engines:
        try:
            result = pipeline.process(
                image_path=args.image,
                household_id=args.household_id,
                ocr_engine=engine,
            )
            apply_year_consistency([result], runtime_config)
            output_path = output_dir / f"{Path(args.image).stem}.{engine}.json"
            write_json(
                output_path,
                payload=result.to_dict(),
                pretty=bool(runtime_config.get("output", {}).get("pretty_json", True)),
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

    runtime_config = _apply_force_cpu_config(config, force_cpu=bool(args.force_cpu), target_engines=engines)
    items: list[dict[str, Any]] = []
    for engine in engines:
        try:
            adapter = create_ocr_adapter(engine, runtime_config)
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


def cmd_refresh_summary(args: argparse.Namespace) -> int:
    target_dir = Path(args.target_dir)
    if not target_dir.exists() or not target_dir.is_dir():
        print(f"target directory not found: {target_dir}")
        return 1
    csv_path = write_summary_csv(target_dir)
    print(f"summary-csv-updated: {csv_path}")
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
    if args.command == "refresh-summary":
        return cmd_refresh_summary(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

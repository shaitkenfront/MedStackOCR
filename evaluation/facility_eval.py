"""医療機関名（支払先）の抽出精度を、本番の領収書で評価する。

使い方（リポジトリのルートで実行）:

    # 1. 評価用データを作る（S3 の画像を OCR し、確定済みの支払先を正解として保存）
    python -m evaluation.facility_eval build --out D:/Temp/medstack-eval

    # 2. 抽出ロジックを評価する（旧版との比較も出す）
    python -m evaluation.facility_eval run --data D:/Temp/medstack-eval --baseline-rev HEAD

build で必要な AWS 権限（CLI ユーザー）:
- s3:ListBucket / s3:GetObject（領収書バケット）
- dynamodb:Query（aggregate-entries テーブルの receipt_id_index）
- secretsmanager:GetSecretValue（Document AI の認証情報。メモリ上だけで使い、ファイルには書かない）

画像そのものは OCR 後に削除し、評価には OCR の行データと正解の名前だけを残す。
すでに `<画像>.ocr.json`（OCR 結果の保存ファイル）がある画像は、OCR を呼び直さずにそれを使う。
家族の医療情報を含むので、評価が終わったら --out のディレクトリを削除すること。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from classify.document_classifier import DocumentClassifier
from core.enums import DocumentType, FieldName
from inbox.ocr_archive import OCR_ARCHIVE_SUFFIX, lines_from_archive
from ledger.providers import are_similar, comparison_key

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES_FILE = "cases.jsonl"
CANDIDATE_THRESHOLD = 2.5


# ---------------------------------------------------------------- build


def build_dataset(out_dir: Path, *, region: str, limit: int | None) -> None:
    import boto3  # type: ignore

    context = json.loads((REPO_ROOT / "infra/cdk/cdk.json").read_text(encoding="utf-8"))["context"]
    bucket = context["receipt_bucket_name"]
    prefix = f"{context.get('receipt_prefix', 'raw')}/"
    table = f"{context.get('prefix', 'medstackocr')}-aggregate-entries"

    s3 = boto3.client("s3", region_name=region)
    ddb = boto3.client("dynamodb", region_name=region)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_path = out_dir / CASES_FILE
    existing = _load_cases(cases_path) if cases_path.exists() else {}

    keys: list[str] = []
    archives: set[str] = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith(OCR_ARCHIVE_SUFFIX):
                archives.add(key[: -len(OCR_ARCHIVE_SUFFIX)])
            else:
                keys.append(key)
    if limit:
        keys = keys[:limit]

    ocr = None
    written = 0
    with cases_path.open("a", encoding="utf-8") as fp:
        for key in keys:
            receipt_id = Path(key).name.split("_", 1)[0]
            if receipt_id in existing:
                continue
            truth = _ground_truth(ddb, table, receipt_id)
            if truth is None:
                print(f"skip {receipt_id}: 明細がありません（削除済みか未確定）")
                continue
            if key in archives:
                body = s3.get_object(Bucket=bucket, Key=key + OCR_ARCHIVE_SUFFIX)["Body"].read()
                archive = json.loads(body)
                lines = archive.get("lines") or []
                source = "archive"
            else:
                if ocr is None:
                    ocr = _DocumentAiRunner(context, region)
                lines = ocr.run(s3, bucket, key)
                source = "documentai"
            case = {"receipt_id": receipt_id, "image_key": key, "source": source, "lines": lines, **truth}
            fp.write(json.dumps(case, ensure_ascii=False) + "\n")
            written += 1
            print(f"added {receipt_id} ({source}, {len(lines)} lines)")
    print(f"done: {written} cases added -> {cases_path}")


def _ground_truth(ddb: Any, table: str, receipt_id: str) -> dict[str, Any] | None:
    response = ddb.query(
        TableName=table,
        IndexName="receipt_id_index",
        KeyConditionExpression="receipt_id = :r",
        ExpressionAttributeValues={":r": {"S": receipt_id}},
        Limit=1,
    )
    items = response.get("Items") or []
    if not items:
        return None
    item = items[0]
    provider = (item.get("provider_name") or {}).get("S")
    status = (item.get("status") or {}).get("S")
    if not provider:
        return None
    return {
        "provider_name": provider,
        "status": status,
        "created_at": (item.get("created_at") or {}).get("S", ""),
    }


class _DocumentAiRunner:
    """Document AI で OCR する。認証情報は Secrets Manager から読み、メモリ上だけで使う。"""

    def __init__(self, context: dict[str, Any], region: str) -> None:
        import boto3  # type: ignore
        from google.api_core.client_options import ClientOptions  # type: ignore
        from google.cloud import documentai  # type: ignore
        from google.oauth2 import service_account  # type: ignore

        from ocr.documentai_adapter import GoogleDocumentAIAdapter
        from ocr.normalizer import OCRNormalizer

        secret = boto3.client("secretsmanager", region_name=region).get_secret_value(
            SecretId=context["app_secrets_name"]
        )
        payload = json.loads(secret["SecretString"])
        credentials_info = payload.get("docai_credentials_json")
        if isinstance(credentials_info, str):
            credentials_info = json.loads(credentials_info)
        credentials = service_account.Credentials.from_service_account_info(credentials_info)

        location = context.get("docai_location", "us")
        self.adapter = GoogleDocumentAIAdapter(
            project_id=context["docai_project_id"],
            location=location,
            processor_id=context["docai_processor_id"],
        )
        self.adapter._client = documentai.DocumentProcessorServiceClient(
            credentials=credentials,
            client_options=ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com"),
        )
        self.normalizer = OCRNormalizer()

    def run(self, s3: Any, bucket: str, key: str) -> list[dict[str, Any]]:
        from io_utils.image_loader import get_image_size

        suffix = Path(key).suffix or ".jpg"
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / f"receipt{suffix}"
            s3.download_file(bucket, key, str(image_path))
            raw = self.adapter.run(str(image_path))
            lines = self.normalizer.normalize(raw=raw, image_size=get_image_size(str(image_path)))
        # with を抜けた時点で画像は削除される
        return [
            {
                "text": line.text,
                "bbox": list(line.bbox),
                "confidence": line.confidence,
                "line_index": line.line_index,
                "page": line.page,
            }
            for line in lines
        ]


# ---------------------------------------------------------------- run


@dataclass
class _Outcome:
    receipt_id: str
    truth: str
    predicted: str | None
    verdict: str  # exact / similar / wrong / none


def run_evaluation(data_dir: Path, *, baseline_rev: str | None, show_cases: bool) -> None:
    cases = list(_load_cases(data_dir / CASES_FILE).values())
    if not cases:
        print("評価用データがありません。先に build を実行してください。")
        return
    classifier = DocumentClassifier()
    variants: list[tuple[str, Any, bool]] = []
    if baseline_rev:
        variants.append((f"旧版({baseline_rev})", _load_extractor_from_git(baseline_rev), False))
    from extractors.facility_extractor import FacilityExtractor

    variants.append(("新版(A: ルールのみ)", FacilityExtractor(), False))
    variants.append(("新版(A+B: 確定済みの支払先あり)", FacilityExtractor(), True))

    all_names = [(case["receipt_id"], case["provider_name"]) for case in cases if case.get("status") == "confirmed"]
    print(f"評価件数: {len(cases)} 件（確定済み {len(all_names)} 件）\n")
    for label, extractor, use_known in variants:
        outcomes: list[_Outcome] = []
        for case in cases:
            lines = lines_from_archive(case)
            document_type, *_ = classifier.classify(lines)
            known = None
            if use_known:
                # 自分自身の正解は使わない（ほかの領収書で確定した名前だけ＝過去の受診歴の代わり）
                known = [name for rid, name in all_names if rid != case["receipt_id"]]
            predicted = _select_payer(extractor, document_type, lines, known)
            outcomes.append(_judge(case, predicted))
        _print_summary(label, outcomes)
        if show_cases:
            for outcome in outcomes:
                if outcome.verdict in ("wrong", "none"):
                    print(f"    [{outcome.verdict}] 正解={outcome.truth} / 抽出={outcome.predicted}")
            print()


def _select_payer(extractor: Any, document_type: DocumentType, lines: list[Any], known: list[str] | None) -> str | None:
    try:
        pool = extractor.extract(document_type, lines, known_names=known)
    except TypeError:  # 旧版は known_names を受け取らない
        pool = extractor.extract(document_type, lines)
    for field_name in (FieldName.PAYER_FACILITY_NAME, FieldName.PRESCRIBING_FACILITY_NAME):
        candidates = sorted(pool.get(field_name) or [], key=lambda c: (c.score, c.ocr_confidence), reverse=True)
        if candidates and candidates[0].score >= CANDIDATE_THRESHOLD:
            # 明細の支払先は、支払先が無いときだけ処方元を使う（集計テーブルと同じ規則）
            return str(candidates[0].value_normalized)
    return None


def _judge(case: dict[str, Any], predicted: str | None) -> _Outcome:
    truth = case["provider_name"]
    if predicted is None:
        verdict = "none"
    elif comparison_key(predicted) == comparison_key(truth):
        verdict = "exact"
    elif are_similar(predicted, truth):
        verdict = "similar"
    else:
        verdict = "wrong"
    return _Outcome(case["receipt_id"], truth, predicted, verdict)


def _print_summary(label: str, outcomes: list[_Outcome]) -> None:
    total = len(outcomes)
    counts = {key: sum(1 for o in outcomes if o.verdict == key) for key in ("exact", "similar", "wrong", "none")}
    rate = (counts["exact"] + counts["similar"]) / total * 100 if total else 0.0
    print(
        f"{label}: 正解 {counts['exact']} / ほぼ正解 {counts['similar']} / "
        f"誤り {counts['wrong']} / 取れず {counts['none']}  → 正解率 {rate:.0f}%"
    )


def _load_extractor_from_git(rev: str) -> Any:
    source = subprocess.run(
        ["git", "show", f"{rev}:extractors/facility_extractor.py"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    spec = importlib.util.spec_from_loader(f"facility_extractor_{rev}", loader=None)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    exec(compile(source, f"{rev}:extractors/facility_extractor.py", "exec"), module.__dict__)
    return module.FacilityExtractor()


def _load_cases(path: Path) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return cases
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            cases[case["receipt_id"]] = case
    return cases


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="評価用データを作る")
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--region", default="ap-northeast-1")
    build.add_argument("--limit", type=int, default=None)
    run = sub.add_parser("run", help="抽出精度を評価する")
    run.add_argument("--data", type=Path, required=True)
    run.add_argument("--baseline-rev", default=None, help="比較する旧版の git リビジョン")
    run.add_argument("--show-cases", action="store_true", help="誤った例を表示する")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.command == "build":
        build_dataset(args.out, region=args.region, limit=args.limit)
    else:
        run_evaluation(args.data, baseline_rev=args.baseline_rev, show_cases=args.show_cases)


if __name__ == "__main__":
    main()

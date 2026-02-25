# MedStackOCR MVP

医療費控除向け領収書抽出エンジンの MVP 実装です。

## 主要機能
- 施設名・日付・金額抽出
- 家族氏名抽出（家族辞書 + alias 対応）
- Resolver による `AUTO_ACCEPT` / `REVIEW_REQUIRED` / `REJECTED`
- 監査情報出力（根拠 `reasons` を保持）
- 世帯ローカルテンプレート（保存・一致・学習）
- CLI: `extract` / `batch` / `compare-ocr` / `learn-template`
- 評価スクリプト（`evaluation.eval_runner`）

## クイックスタート
```bash
pip install -r requirements-ocr-optional.txt
```

公開運用の推奨:
- `config.example.yaml` をコピーして `config.yaml` を作成し、家族氏名・APIキー等をローカルで設定
- `config.yaml` / `data/templates/` は `.gitignore` 対象（個人情報・学習済みテンプレートの混入防止）

`config.yaml` 既定では `ocr.allowed_engines: [yomitoku]` のため、`--ocr-engine` に他エンジンを指定するとエラーになります。
`yomitoku` は、CUDA が使えない環境では自動で CPU にフォールバックします。強制的に CPU 実行したい場合は `--force-cpu` を指定してください。
`--household-id` は任意です（指定時のみテンプレート照合に利用）。

`family_registry` は必須です。`members` に家族氏名（`canonical_name`）と OCR 揺れ向けの `aliases` を登録してください。
未登録氏名を検出した場合は以下で判定します。
- 同姓: `REVIEW_REQUIRED`
- 異姓: `REJECTED`

テンプレートと運用手順:
- `family_registry.template.yaml`
- `docs/family_registry_guide.md`

年整合チェック（`batch` 実行時）:
- `pipeline.target_tax_year` を指定した場合: `payment_date.year` が一致しない明細を `REVIEW_REQUIRED`
- 未指定の場合: 同一バッチ内で最多年（重み付き多数決）を推定し、外れ年を `REVIEW_REQUIRED`
- 設定は `pipeline.year_consistency`（`enabled`, `min_samples`, `dominant_ratio_threshold`, `weight_by_confidence`）

`batch` の出力:
- `summary.json`: 当回実行の処理結果（`SKIPPED_ALREADY_PROCESSED` を含む）
- `summary.csv`: 全 `*.result.json` から再生成する4項目サマリ（`日付`,`氏名`,`医療機関・調剤薬局名`,`金額`）
- `processed_files.json`: 処理済み画像の管理ファイル（サイズ + 更新時刻）
  - 2回目以降は `processed_files.json` と一致する画像をスキップし、未処理ファイルのみ実行
- `--target-dir`: 入力画像（`*.jpg` など）と出力ファイル（`*.result.json`, `summary.*`）を同じフォルダで管理

通知（`batch` 実行時に新規追加領収書を検知）:
- 通知先は `notifications.channels` で選択（`line`, `slack`, `discord`）
- 実際の送信処理は抽象化されており、本体ロジックと疎結合
- `processed_files.json` に未登録の画像がある場合のみ通知

```yaml
notifications:
  enabled: true
  channels: [slack, discord]  # line/slack/discord から選択
  max_items_in_message: 10
  slack:
    webhook_url: "https://hooks.slack.com/services/..."
  discord:
    webhook_url: "https://discord.com/api/webhooks/..."
  line:
    channel_access_token: "YOUR_LINE_CHANNEL_ACCESS_TOKEN"
    to: "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

```bash
python -m app.main extract \
  --config config.yaml \
  --image data/samples/pharmacy_receipt.jpg \
  --household-id household_demo  # 任意 \
  --force-cpu \
  --output data/outputs/pharmacy_result.json
```

```bash
python -m app.main batch \
  --config config.yaml \
  --household-id household_demo  # 任意 \
  --force-cpu \
  --target-dir data/samples
```

```bash
python -m app.main refresh-summary \
  --config config.yaml \
  --target-dir data/outputs/yomitoku_tuned
```

```bash
python -m app.main compare-ocr \
  --config config.yaml \
  --image data/samples/pharmacy_receipt.jpg \
  --household-id household_demo  # 任意 \
  --ocr-engines yomitoku \
  --force-cpu \
  --target-dir data/outputs/compare
```

`yomitoku` と `documentai` を比較する場合は、事前に `config.yaml` の許可エンジンと `documentai` 設定を追加します。

```yaml
ocr:
  allowed_engines:
    - yomitoku
    - documentai
  engines:
    documentai:
      enabled: true
      project_id: your-gcp-project-id
      location: us
      processor_id: your-processor-id
      credentials_path: C:\\path\\to\\service-account.json
      amount_tuning:
        label_alignment_bonus_max: 3.0
        near_secondary_without_currency_penalty: 1.8
```

```bash
python -m app.main compare-ocr \
  --config config.yaml \
  --image data/samples/pharmacy_receipt.jpg \
  --ocr-engines yomitoku,documentai \
  --target-dir data/outputs/compare
```

```bash
python -m app.main healthcheck-ocr \
  --config config.yaml \
  --ocr-engines yomitoku \
  --force-cpu
```

```bash
python -m app.main learn-template \
  --config config.yaml \
  --document-result data/outputs/pharmacy_result.json \
  --review-correction data/outputs/review_fix.example.json
```

```bash
python -m evaluation.eval_runner \
  --pred-dir data/outputs \
  --gt-dir data/ground_truth \
  --output data/outputs/metrics.json
```

## テスト
```bash
python -m unittest discover -s tests -p "test_*.py"
```

## エンジン別メモ
- `tesseract`: Windows の場合 `C:\Program Files\Tesseract-OCR\tesseract.exe` を既定参照。日本語用に `data/tessdata/jpn.traineddata` を参照。
- `paddle`: `paddleocr` + `paddlepaddle` が必要。GPU利用時は CUDA 対応ビルドが必要。
- `yomitoku`: 画像は OpenCV で読み込み。`device: cuda` でも CUDA 非対応 Torch の場合は CPU fallback。
- `deepseek`: `backend: api|local` を選択可能。
  - `api`: `DS_OCR_API_KEY` が必要（`deepseek-ocr` パッケージ経由）。
  - `local`: `torch` + `transformers` が必要（`deepseek-ai/DeepSeek-OCR` をローカル推論）。
- `documentai`: `google-cloud-documentai` が必要。`project_id` / `processor_id` / 認証情報（`GOOGLE_APPLICATION_CREDENTIALS` または `credentials_path`）を設定。
  - 金額抽出の専用調整は `ocr.engines.documentai.amount_tuning` で変更可能。

`config.yaml` 例（ローカル推論）:
```yaml
ocr:
  engines:
    deepseek:
      enabled: true
      backend: local
      model_name: deepseek-ai/DeepSeek-OCR
      local_prompt: "<image>\nFree OCR."
      local_base_size: 512
      local_image_size: 512
      local_crop_mode: false
      local_device: cuda
      local_dtype: bfloat16
      local_attn_impl: eager
```

## Paddle 再検証 (Python 3.12)
以下の固定構成で動作確認済み（CPU実行）:
- `paddlepaddle==3.2.2`
- `paddleocr==3.3.3`

※この検証を行う場合は、一時的に `config.yaml` の `ocr.allowed_engines` に `paddle` を追加してください。

```bash
D:\\ProgramData\\Anaconda\\python.exe -m venv .venv-py312-paddle
.venv-py312-paddle\\Scripts\\python.exe -m pip install -r requirements-paddle-py312.txt
.venv-py312-paddle\\Scripts\\python.exe -m app.main extract \
  --config config.yaml \
  --image data/samples/clinic_receipt.jpg \
  --household-id household_demo  # 任意 \
  --ocr-engine paddle \
  --output data/outputs/clinic_paddle_py312.json
```

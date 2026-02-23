# MedStackOCR MVP

`PLAN.md` に基づく、医療費控除向け領収書抽出エンジンの MVP 実装です。

## 主要機能
- OCR 抽象化（`mock` / `tesseract` / `paddle` / `yomitoku` / `deepseek`）
- OCR 正規化（`OCRLine` 共通化）
- 帳票分類（`pharmacy` / `clinic_or_hospital` / `unknown`）
- 施設名・日付・金額抽出
- Resolver による `AUTO_ACCEPT` / `REVIEW_REQUIRED` / `REJECTED`
- 監査情報出力（根拠 `reasons` を保持）
- 世帯ローカルテンプレート（保存・一致・学習）
- CLI: `extract` / `batch` / `compare-ocr` / `learn-template`
- 評価スクリプト（`evaluation.eval_runner`）

## クイックスタート
```bash
pip install -r requirements-ocr-optional.txt
```

```bash
python -m app.main extract \
  --config config.yaml \
  --image data/samples/pharmacy_receipt.jpg \
  --household-id household_demo \
  --ocr-engine mock \
  --output data/outputs/pharmacy_result.json
```

```bash
python -m app.main batch \
  --config config.yaml \
  --input-dir data/samples \
  --household-id household_demo \
  --ocr-engine mock \
  --output-dir data/outputs
```

```bash
python -m app.main compare-ocr \
  --config config.yaml \
  --image data/samples/pharmacy_receipt.jpg \
  --household-id household_demo \
  --ocr-engines mock,tesseract,paddle,yomitoku,deepseek \
  --output-dir data/outputs/compare
```

```bash
python -m app.main healthcheck-ocr \
  --config config.yaml \
  --ocr-engines mock,tesseract,paddle,yomitoku,deepseek
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

```bash
D:\\ProgramData\\Anaconda\\python.exe -m venv .venv-py312-paddle
.venv-py312-paddle\\Scripts\\python.exe -m pip install -r requirements-paddle-py312.txt
.venv-py312-paddle\\Scripts\\python.exe -m app.main extract \
  --config config.yaml \
  --image data/samples/clinic_receipt.jpg \
  --household-id household_demo \
  --ocr-engine paddle \
  --output data/outputs/clinic_paddle_py312.json
```

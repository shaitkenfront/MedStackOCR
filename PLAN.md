# PLAN.md — 医療費控除向け 領収書抽出エンジン MVP

## 目的

雑多な医療機関・調剤薬局の領収書画像から、医療費控除に必要な以下の項目を抽出する MVP を実装する。

- 支払先名（病院/クリニック/薬局）
- 日付（支払日寄り）
- 支払額（請求額/領収額）

加えて、以下を実現する。

- 調剤薬局の「薬局名」と「処方元医療機関名」を区別する
- 自信が低い場合は `REVIEW_REQUIRED` として保留できる
- ユーザー修正結果を「世帯ローカルテンプレート」として学習できる土台を作る
- OCRエンジンを抽象化し、複数方式を切り替えて比較できる

---

## スコープ

### MVPで実装する
- OCR抽象化インターフェース
- OCR結果の共通正規化（行単位 + bbox + confidence）
- 帳票タイプ分類（最低: `pharmacy`, `clinic_or_hospital`, `unknown`）
- 候補抽出（施設名、日付、金額）
- 候補解決（スコアリング + 閾値）
- 要確認判定（`AUTO_ACCEPT` / `REVIEW_REQUIRED` / `REJECTED`）
- 監査ログ（根拠の保存）
- 世帯ローカルテンプレートの保存（最小構成）
- CLIでの実行（単一画像/複数画像）
- 評価用の簡易スクリプト（正解データとの比較）

### MVPで後回し
- Web UI / モバイルUI
- グローバルテンプレート共有
- 高度な類似検索（画像特徴ベース）
- 学習モデル（LightGBM / LayoutLM等）
- 個人情報マスキング・暗号化・認可などの非機能要件（設計メモのみ）

---

## 成功条件（MVP完了条件）

1. CLIで画像を入力し、JSONで抽出結果を返せる
2. OCRエンジンを設定で切り替えられる（少なくとも `mock` + 1実装）
3. 調剤薬局レシートで「薬局名」と「処方元医療機関名」を別フィールドで出せる
4. 抽出値に対して、元テキスト・bbox・confidence・判定理由が残る
5. ユーザー修正（JSON入力で可）から、世帯ローカルテンプレートを保存できる
6. 次回処理時、世帯テンプレートを優先適用できる（最低限のアンカー+相対位置）

---

## 基本アーキテクチャ

```text
input image
  -> OCR Adapter (swappable)
  -> OCR Normalizer (common line format)
  -> Document Classifier
  -> Template Matcher (household local; MVPは簡易版)
  -> Generic Extractors (facility/date/amount)
  -> Resolver (final decision + confidence)
  -> Audit Logger
  -> JSON output
```

### OCR抽象化ポリシー（重要）

OCR呼び出しは抽象化し、オープンウェイト／商用クラウドを自由に切り替えられるようにする。

- 例:
    - TesseractAdapter
    - PaddleOCRAdapter
    - GoogleVisionAdapter
    - DocumentAIAdapter
    - MockOCRAdapter（テスト用）

上位ロジック（分類・抽出・解決）は OCR 実装に依存しないこと。

## ディレクトリ構成（提案）

```text
receipt_extractor/
  app/
    main.py                  # CLI entry
    config.py
  core/
    models.py                # dataclass / pydantic models
    enums.py
  ocr/
    base.py                  # OCRAdapter interface
    mock_adapter.py
    paddle_adapter.py        # optional stub if not installed
    tesseract_adapter.py     # optional stub if not installed
    normalizer.py
  classify/
    document_classifier.py
  templates/
    fingerprint.py           # MVP: anchor-based simple fingerprint
    matcher.py
    learner.py               # save template from user corrections
    store.py                 # file-based JSON store
  extractors/
    facility_extractor.py
    date_extractor.py
    amount_extractor.py
    common.py
  resolver/
    decision_resolver.py
    confidence.py
  audit/
    logger.py
  io/
    image_loader.py
    json_writer.py
  evaluation/
    eval_runner.py
    metrics.py
  tests/
    ...
data/
  samples/
  ground_truth/
  templates/
  outputs/
docs/
  PLAN.md
  schema_examples/
```

## データモデル（MVP）

### 1) 共通OCR行モデル

```json
{
  "text": "〇〇薬局",
  "bbox": [x1, y1, x2, y2],
  "polygon": [[x,y], [x,y], [x,y], [x,y]],
  "confidence": 0.93,
  "line_index": 3,
  "page": 1
}
```

- `bbox` は正規化座標（0.0〜1.0）を推奨（画像サイズ差異に強い）
- OCRエンジン固有の値は `raw` に保持してもよい

### 2) 抽出候補モデル

```json
{
  "field": "payer_facility_name",
  "value_raw": "〇〇調剤薬局",
  "value_normalized": "〇〇調剤薬局",
  "source_line_indices": [3],
  "bbox": [0.08, 0.05, 0.55, 0.11],
  "score": 8.2,
  "ocr_confidence": 0.93,
  "reasons": [
    "contains_keyword:薬局",
    "top_region_bonus",
    "near_anchor:TEL"
  ]
}
```

### 3) 最終結果モデル

```json
{
  "document_id": "2026-02-22_xxx",
  "household_id": "household_demo",
  "document_type": "pharmacy",
  "template_match": {
    "matched": true,
    "template_family_id": "pharmacy_family_001",
    "score": 0.87
  },
  "fields": {
    "payer_facility_name": { "...candidate-like..." : "..." },
    "prescribing_facility_name": { "...candidate-like..." : "..." },
    "payment_date": { "...candidate-like..." : "..." },
    "payment_amount": { "...candidate-like..." : "..." }
  },
  "decision": {
    "status": "AUTO_ACCEPT",
    "confidence": 0.91,
    "reasons": ["all_required_fields_present", "template_match_strong"]
  },
  "audit": {
    "engine": "paddleocr",
    "engine_version": "x.y.z",
    "pipeline_version": "0.1.0"
  }
}
```

### 4) 世帯ローカルテンプレート（MVP）
```json
{
  "template_family_id": "pharmacy_family_001",
  "scope": "household",
  "household_id": "household_demo",
  "document_type": "pharmacy",
  "anchors": [
    {
      "text_pattern": "領収書",
      "bbox": [0.40, 0.02, 0.62, 0.07]
    },
    {
      "text_pattern": "TEL",
      "bbox": [0.05, 0.10, 0.20, 0.14]
    },
    {
      "text_pattern": "処方箋",
      "bbox": [0.08, 0.35, 0.30, 0.40]
    }
  ],
  "field_specs": {
    "payer_facility_name": {
      "target_bbox": [0.05, 0.03, 0.70, 0.10],
      "anchor_refs": ["領収書", "TEL"],
      "selection_rules": ["topmost_text", "prefer_keyword:薬局,調剤"]
    },
    "prescribing_facility_name": {
      "target_bbox": [0.05, 0.30, 0.90, 0.42],
      "anchor_refs": ["処方箋"],
      "selection_rules": ["prefer_near_anchor", "prefer_keyword:病院,医院,クリニック"]
    },
    "payment_date": {
      "target_bbox": [0.05, 0.15, 0.95, 0.30],
      "selection_rules": ["prefer_label:領収日,発行日,調剤日", "parse_date"]
    },
    "payment_amount": {
      "target_bbox": [0.45, 0.70, 0.98, 0.98],
      "selection_rules": ["prefer_label:領収,請求,お支払,合計", "parse_amount"]
    }
  },
  "sample_count": 3,
  "success_rate": 0.92
}
```

### OCR抽象化インターフェース仕様（必須）

#### `OCRAdapter` interface

```python
class OCRAdapter(Protocol):
    name: str

    def run(self, image_path: str) -> OCRRawResult:
        ...

    def healthcheck(self) -> bool:
        ...
```

#### `OCRNormalizer` interface

```python
class OCRNormalizer:
    def normalize(self, raw: OCRRawResult, image_size: tuple[int, int]) -> list[OCRLine]:
        ...
```

#### 実装ルール

- OCR adapter は「OCRして生データを返す」だけ

- 正規化（行結合、bbox正規化、confidence変換）は `normalizer` 側

- 上位ロジックは `OCRLine` のみ扱う

- OCRエンジンの切替は設定ファイル or CLI引数で行う

    - 例: `--ocr-engine mock|paddle|tesseract|vision|documentai`

#### 実験しやすさの要件

- 同一画像に対し複数OCRエンジンを一括実行できるCLIを用意

- 出力JSONに OCRエンジン名・バージョンを残す

- 評価スクリプトで engine別の精度比較ができる

---

### 帳票分類ルール（MVP）

OCR行テキストから帳票タイプを判定する。

#### ラベル

- `pharmacy`
- `clinic_or_hospital`
- `unknown1

#### ルール（初版）

`pharmacy` に強く寄るキーワード:

- 薬局
- 調剤
- 処方箋
- 保険薬局

`clinic_or_hospital` に寄るキーワード:

- 病院
- 医院
- クリニック
- 診療所

スコア差が小さい、または OCR低品質なら `unknown`

### 抽出ロジック（MVP）

#### 1) 施設名抽出

#### フィールド

- `payer_facility_name`（支払先: 病院 or 薬局）
- `prescribing_facility_name`（調剤薬局時のみ候補抽出）

#### 方針

- 単一「医療機関名」ではなく、役割別に抽出
- 候補を複数作成し、スコアリングで選ぶ

#### 施設名候補スコア（初版例）
`payer_facility_name`（薬局帳票時）
- `+3` 行に `薬局|ファーマシー|調剤`
- `+2` 上部25%領域
- `+2` 近傍に `〒|TEL|領収書|発行`
- `-4` 近傍に `処方箋|保険医療機関|交付|医師`
- `-2` `病院|医院|クリニック`（例外あり）

`prescribing_facility_name`（薬局帳票時）

- `+3` 近傍に `処方箋|保険医療機関|交付`
- `+2` `病院|医院|クリニック`
- `-3` `薬局|調剤`

`payer_facility_name`（病院帳票時）

- `+3` 上部25%領域
- `+2` `病院|医院|クリニック|診療所`
- `+1` 近傍に `TEL|〒|領収書`
- `-2` `処方箋`

### 2) 日付抽出
#### 目標

支払日寄りの日付を抽出する（領収日・発行日・調剤日優先）

#### 受理する形式

- `YYYY/MM/DD`
- `YYYY-MM-DD`
- `YYYY年M月D日`
- `R8.2.22` 等の和暦（対応できる範囲で）
- `令和8年2月22日` 等の和暦（対応できる範囲で）
- 区切り崩れ（スペース）も許容

#### 優先ラベル

- `領収日`, `発行日`, `調剤日`, `お会計日`
- `ラベルなし日付（上部〜中部優先）
- `処方箋交付日`, `受診日`

#### 追加ルール

- 未来日すぎる値は減点（実行日基準）
- 年の欠落時は保留候補として扱う（MVPでは自動補完しない）

---

### 3) 金額抽出
#### 目標

「実際に支払った額」を抽出する

#### 優先ラベル

- 最優先: `領収`, `請求`, `お支払`, `今回`
- 次点: `合計`, `計

#### 除外

- `点`（保険点数）
- `消費税`, `税率`, `%`
- `総点数` など点数系文脈
- 明細行のみの金額（ラベルなし小額群）※候補には残しても低スコア

### 正規化

- `¥`, `円`, `,` を正規化して整数に
- 0円は通常減点（無料検診等の例外あり）

---

### 解決ロジック（Resolver）

候補抽出後、各フィールドごとに最終値を決定する。

#### 決定方針

- 各フィールドで最高スコア候補を採用
- ただし閾値未満は未確定
- 必須フィールドが揃わない場合 `REVIEW_REQUIRED`
- OCR全体品質が低い・候補ゼロなら `REJECTED`

#### ステータス

- `AUTO_ACCEPT`
- `REVIEW_REQUIRED`
- `REJECTED`

#### 信頼度（MVP）

単純な加重平均でよい：

- OCR confidence
- 候補スコア（正規化）
- テンプレート一致スコア
- 妥当性チェック結果

---

### 世帯ローカルテンプレート（MVP仕様）
#### 目的

ユーザー修正結果から、次回以降の抽出精度を上げる。

#### 学習トリガー

- `REVIEW_REQUIRED` の修正完了時
- `AUTO_ACCEPT` でもユーザー手修正が入った時

#### MVPで保存する情報

- `document_type`
- 主要アンカー（ユーザー指定領域近傍の代表語）
- フィールドの相対bbox
- 選択ルールヒント（キーワード優先など）
- 成功/失敗統計

#### 次回適用（MVP）

1. 同一 `household_id` のテンプレート群を取得
2. アンカー一致率 + 位置一致率でスコア計算
3. 閾値以上ならテンプレート field_specs を使って候補探索を優先
4. テンプレート結果が弱ければ汎用抽出にフォールバック

---

### CLI仕様（MVP）
#### 単一画像処理

```bash
python -m app.main extract \
  --image path/to/receipt.jpg \
  --household-id household_demo \
  --ocr-engine paddle \
  --output data/outputs/result.json
```

#### 複数画像処理

```bash
python -m app.main batch \
  --input-dir data/samples \
  --household-id household_demo \
  --ocr-engine paddle \
  --output-dir data/outputs
```

#### OCR比較実験

```bash
python -m app.main compare-ocr \
  --image path/to/receipt.jpg \
  --household-id household_demo \
  --ocr-engines mock,paddle,tesseract \
  --output-dir data/outputs/compare
```

#### テンプレート学習（修正JSONから）

```bash
python -m app.main learn-template \
  --document-result data/outputs/result.json \
  --review-correction data/outputs/review_fix.json
```

---
### 設定ファイル（例）

`config.yaml` 例:

```yaml
pipeline:
  review_threshold: 0.72
  reject_threshold: 0.35

ocr:
  engine: paddle
  engines:
    paddle:
      enabled: true
      lang: ja
    tesseract:
      enabled: false
      lang: jpn
    vision:
      enabled: false
      credentials_env: GOOGLE_APPLICATION_CREDENTIALS
    documentai:
      enabled: false
      project_id: ""
      processor_id: ""

templates:
  store_path: data/templates
  household_match_threshold: 0.65

output:
  save_audit: true
  pretty_json: true
```

---

### テスト計画
#### ユニットテスト

- 日付正規化（和暦含む最低限）
- 金額正規化
- キーワードスコアリング
- 帳票分類
- テンプレート一致（簡易）

#### 結合テスト

- `MockOCRAdapter` + サンプルOCR JSONで end-to-end
- `REVIEW_REQUIRED` 判定分岐
- テンプレート学習後の再処理で精度改善確認

##### 回帰テスト

- `data/ground_truth/` の正解データに対し、項目一致率を計測
- OCR engine別に比較可能にする

---

### 評価指標（最低限）
#### 項目別

- `payer_facility_name` 完全一致率
- `payment_date` 一致率（ISO正規化後）
- `payment_amount` 一致率（整数）

#### 運用寄り（重要）

- `AUTO_ACCEPT` 件数
- `AUTO_ACCEPT` の正解率
- `REVIEW_REQUIRED` 率
- `REJECTED-  率

#### テンプレート効果

- 同一世帯・同フォーマット再処理時の改善率

---

### 実装順序（Codex向けタスク分解）
#### Phase 1: 骨格

1. プロジェクト雛形作成
2. `OCRAdapter` interface + `MockOCRAdapter`
3. `OCRLine`, `Candidate`, `ExtractionResult` 等のモデル定義
4. CLI骨格（`extract` コマンド）

#### Phase 2: 汎用抽出

5. OCR normalizer（Mock前提でも可）
6. 帳票分類（キーワードベース）
7. 日付抽出器
8. 金額抽出器
9. 施設名抽出器（薬局/病院の役割分離）
10. Resolver + status判定
11. 監査ログ出力

#### Phase 3: テンプレート最小実装

12. テンプレートJSONストア（file-based）
13. 簡易テンプレートマッチャ（アンカー+相対位置）
14. 修正JSONからテンプレート学習
15. テンプレート優先抽出の統合

#### Phase 4: OCR差し替え実験基盤

16. `PaddleOCRAdapter` or `TesseractAdapter` の実装（1つで可）
17. `compare-ocr` コマンド実装
18. engine/version を監査ログに記録

#### Phase 5: 評価

19. ground truth フォーマット定義
20. 評価スクリプト + metrics
21. サンプル数十件でベースライン測定

---

### レビュー修正データ仕様（MVP）

ユーザーUI未実装のため、MVPでは JSON で修正入力できればよい。

`review_fix.json` 例:

```json
{
  "document_id": "2026-02-22_xxx",
  "household_id": "household_demo",
  "corrections": {
    "payer_facility_name": {
      "value": "〇〇調剤薬局",
      "bbox": [0.06, 0.04, 0.66, 0.10]
    },
    "prescribing_facility_name": {
      "value": "△△内科クリニック",
      "bbox": [0.08, 0.33, 0.72, 0.39]
    },
    "payment_date": {
      "value": "2026-02-22",
      "bbox": [0.51, 0.18, 0.93, 0.23]
    },
    "payment_amount": {
      "value": 1840,
      "bbox": [0.62, 0.88, 0.95, 0.94]
    }
  }
}
```

---

### 設計メモ（非機能要件の将来拡張）

MVPでは実装しないが、設計上は以下を意識してコードを分離する。

- 個人情報保護（暗号化 at-rest / in-transit）
- OCRクラウド利用時の送信同意管理
- ローカルOCRのみモード
- ログ匿名化（グローバルテンプレート化時）
- 世帯データの削除・エクスポート

---

### 注意事項（Codex向け）

- まずは 精度100%を狙わない。`REVIEW_REQUIRED` を適切に返せることを優先。
- OCRエンジン固有コードを上位層に漏らさない。
- 調剤薬局は「薬局名」と「処方元医療機関名」を分ける。
- 監査ログ（根拠保存）を省略しない。
- 画像処理やOCR依存が重い箇所は、インターフェースとスタブ実装を先に作る。
- すべての主要判断に `reasons` を残す（後の改善速度が上がる）。

---

### 最初の実装対象（今日やること）

1. `core/models.py` と `ocr/base.py` を作る
2. `MockOCRAdapter` で固定OCR行データを返す
3. `date_extractor.py`, `amount_extractor.py` を先に作る
4. `facility_extractor.py` で薬局/処方元の分離を実装
5. `resolver.py` で `AUTO_ACCEPT/REVIEW_REQUIRED/REJECTED` を返す
6. `extract` CLIで JSON を出力する


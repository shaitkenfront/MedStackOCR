# MedStackOCR MVP

医療費控除向け領収書抽出エンジンの MVP 実装です。  
このブランチは **LINE Webhook 起動専用** です。

## 主要機能
- LINE Webhook 経由で画像受付
- 施設名・日付・金額抽出
- 家族氏名抽出（LINEユーザーIDごとの family registry + alias）
- 判定（`AUTO_ACCEPT` / `REVIEW_REQUIRED` / `REJECTED`）
- 会話型修正フロー（`inbox/`）

## インストール
```bash
pip install -r requirements.txt
```

## 設定
1. `config.example.yaml` をコピーして `config.yaml` を作成
2. `ocr.engines.documentai` を設定
3. `line_messaging` と `inbox` を設定
4. DynamoDB運用時は `family_registry.members` は初期値のままで可（実運用はLINE登録フローで保存）

`documentai` 最低限の設定例:
```yaml
ocr:
  engine: documentai
  allowed_engines:
    - documentai
  engines:
    documentai:
      enabled: true
      project_id: your-gcp-project-id
      location: us
      processor_id: your-processor-id
      credentials_path: C:\\path\\to\\service-account.json
```

`line_messaging` 設定例:
```yaml
line_messaging:
  enabled: true
  channel_secret: "YOUR_LINE_CHANNEL_SECRET"
  channel_access_token: "YOUR_LINE_CHANNEL_ACCESS_TOKEN"
  webhook_path: "/webhook/line"
  api_base_url: "https://api.line.me"
  data_api_base_url: "https://api-data.line.me"
  timeout_sec: 10
  allowed_user_ids: []
  default_household_id:

inbox:
  backend: sqlite  # sqlite or dynamodb
  sqlite_path: "data/inbox/linebot.db"
  dynamodb:
    region: ap-northeast-1
    table_prefix: medstackocr
    event_ttl_days: 7
    tables:
      event_dedupe: medstackocr-line-event-dedupe
      receipts: medstackocr-receipts
      receipt_fields: medstackocr-receipt-fields
      sessions: medstackocr-sessions
      aggregate_entries: medstackocr-aggregate-entries
      family_registry: medstackocr-family-registry
  image_store_dir: "data/inbox/images"
  image_retention_days: 14
  session_ttl_minutes: 60
  max_candidate_options: 3
  enable_text_commands: true
```

## 起動（本番）
- AWS Lambda + API Gateway で起動します（ローカルWebhookサーバーはありません）。
- デプロイは `infra/cdk/README.md` を参照してください。

## LINEで使えるテキストコマンド
- `今年の医療費`
- `今月の医療費`
- `未確認`
- `ヘルプ`

## 初回オンボーディング
- 友だち追加（`follow` イベント）時に家族氏名登録フローを開始します。
- 登録案内: 「ご家族の名前を教えてください。カタカナや、良く間違えられる漢字も登録しておくと認識の精度が上ります。」
- クイックリプライで `家族氏名の登録を終了` を提示します。
- `家族氏名の登録を終了` するまで、通常のOCR処理には進みません。
- ブロック（`unfollow`）時は、そのLINEユーザーに紐づくDB情報と保存画像を削除します。

## テスト
```bash
python -m unittest discover -s tests -p "test_*.py"
```

## 補足
- `python -m app.main` の CLI モード（`extract` / `batch` など）は廃止済みです。
- `FastAPI/Uvicorn` のローカルWebhookサーバー運用は廃止済みです。
- AWS Lambda 2段構成の骨格は以下です。
  - `app/lambda_handlers/ingress_handler.py`
  - `app/lambda_handlers/worker_handler.py`
  - `infra/cdk/`（API Gateway / Lambda / SQS / DynamoDB / S3）
- 家族氏名辞書は DynamoDB（ユーザーごと）に保存します。
- CDKのデプロイ手順は `infra/cdk/README.md` を参照してください。
- 機密値（LINEトークン / DocAI認証情報）は Secrets Manager から実行時に取得する構成です。

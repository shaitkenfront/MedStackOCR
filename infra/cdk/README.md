# CDK 雛形 (AWS + Google Document AI Hybrid)

このディレクトリは以下を作成する Python CDK 雛形です。

- API Gateway (HTTP API)
- Lambda `line-ingress` / `line-worker`
- SQS FIFO + DLQ
- DynamoDB 8テーブル（家族氏名辞書・学習ルール・OCR利用制限テーブル含む）
- S3（領収書画像保存）

## 1. 依存インストール
```bash
cd infra/cdk
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

## 2. デプロイ前設定
`cdk.json` の `context`（非機密）と、Secrets Manager（機密）で設定します。

- `prefix`
- `region`
- `line_webhook_path`
- `receipt_bucket_name`
- `app_secrets_name`
- `docai_project_id`
- `docai_location`
- `docai_processor_id`
- `line_login_channel_id`（LIFF の明細画面を使う場合。後述）

Secrets Manager のシークレット文字列(JSON)例:
```json
{
  "line_channel_secret": "YOUR_LINE_CHANNEL_SECRET",
  "line_channel_access_token": "YOUR_LINE_CHANNEL_ACCESS_TOKEN",
  "docai_credentials_json": {
    "type": "service_account",
    "project_id": "YOUR_PROJECT_ID"
  }
}
```

シークレット作成/更新例:
```bash
aws secretsmanager create-secret \
  --name linebot/counselingroomy/prod/app-secrets \
  --secret-string file://app-secrets.json

aws secretsmanager put-secret-value \
  --secret-id linebot/counselingroomy/prod/app-secrets \
  --secret-string file://app-secrets.json
```

## 3. DocAI依存Layerの作成
`line-worker` は `google-cloud-documentai` を Lambda Layer から読み込みます。
デプロイ前に1回実行してください。

```powershell
powershell -File infra/cdk/scripts/build_docai_layer.ps1
```

## 4. デプロイ
```bash
cdk bootstrap
cdk deploy
```

デプロイ後、`LineWebhookUrl` の出力を LINE Developers Console の Webhook URL に設定してください。
家族氏名辞書は `FamilyRegistryTableName` 出力の DynamoDB テーブルに保存されます。
訂正学習ルールは `LearningRulesTableName` 出力の DynamoDB テーブルに保存されます。
OCR利用制限カウンタは `OcrUsageGuardTableName` 出力の DynamoDB テーブルに保存されます。
このテーブルは KMS カスタマーマネージドキーで暗号化されます。

このリポジトリでは非機密の実環境値を `cdk.json` に設定済みです。
- `region`: `ap-northeast-1`
- `prefix`: `medstackocr`
- `receipt_bucket_name`: `medstackocr-027496193801-apne1-receipts`
- `app_secrets_name`: `medstackocr/prod/app-secrets`
- `docai_project_id`: `messtackocr`
- `docai_location`: `us`
- `docai_processor_id`: `54aaada2a941413`

## 5. 明細画面（LIFF）用 API

`ledger-api` Lambda が `/api/*` を処理します（同じ HTTP API に同居）。デプロイ後の `LedgerApiBaseUrl` 出力がベース URL です。

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET | `/api/meta` | 年の一覧・医療費の区分・登録済みの家族 |
| GET | `/api/entries?year=YYYY` | 明細一覧と集計 |
| POST | `/api/entries` | 手入力の明細を追加（交通費など） |
| GET / PATCH / DELETE | `/api/entries/{entry_id}` | 明細の取得・修正・削除（OCR 由来なら原本画像も削除） |
| GET | `/api/checks?year=YYYY` | 抜け漏れチェック |

認証は LIFF の ID トークン（`Authorization: Bearer <liff.getIDToken()>`）を LINE の検証 API で確認します。

### LINE Login チャネルの準備
1. LINE Developers Console で、Messaging API チャネルと**同じプロバイダー**に LINE Login チャネルを作成する。
   別のプロバイダーだとユーザー ID が一致せず、LINE で登録した明細が画面に表示されません。
2. チャネル基本設定の「チャネル ID」を `cdk.json` の `line_login_channel_id` に設定する（非機密値）。
3. 未設定のままデプロイすると、API はすべてのリクエストを 401 で拒否します。

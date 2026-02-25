# MedStackOCR MVP

医療費控除向け領収書抽出エンジンの MVP 実装です。  
このブランチは **LINE Webhook 起動専用** です。

## 主要機能
- LINE Webhook 経由で画像受付
- 施設名・日付・金額抽出
- 家族氏名抽出（family registry + alias）
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
4. `family_registry.members` を実データに更新

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
  sqlite_path: "data/inbox/linebot.db"
  image_store_dir: "data/inbox/images"
  image_retention_days: 14
  session_ttl_minutes: 60
  max_candidate_options: 3
  enable_text_commands: true
```

## 起動
```bash
uvicorn app.line_webhook:app --host 0.0.0.0 --port 8000
```

ヘルスチェック:
```bash
curl http://127.0.0.1:8000/healthz
```

## LINEで使えるテキストコマンド
- `今年の医療費`
- `今月の医療費`
- `未確認`
- `ヘルプ`

## テスト
```bash
python -m unittest discover -s tests -p "test_*.py"
```

## 補足
- `python -m app.main` の CLI モード（`extract` / `batch` など）は廃止済みです。

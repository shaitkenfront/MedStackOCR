# PLAN_LineBot.md — Google Document AI OCR x LINE Messaging API「医療費の会話型インボックス」MVP

## 0. この計画の前提（現行プロジェクト整合）

この計画は、既存の `MedStackOCR` コードベースを作り直さずに拡張する前提です。

- 既存の主軸は CLI パイプライン (`app/main.py`, `app/pipeline.py`)
- OCR は adapter 抽象化済みで、`ocr/documentai_adapter.py` が既に実装済み
- 抽出・判定ロジックは既存実装を利用
  - `FieldName`: `payer_facility_name`, `prescribing_facility_name`, `payment_date`, `payment_amount`, `family_member_name`
  - `DecisionStatus`: `AUTO_ACCEPT`, `REVIEW_REQUIRED`, `REJECTED`
- LINE 送信は通知用途として `notifications/channels.py` に Push 実装済み（会話用 Reply API は未実装）
- 今回は「新規プロダクトを別実装」ではなく「既存エンジンに LINE 会話層を追加」する

---

## 1. 目的

LINE で受信した医療費領収書画像を Google Document AI OCR で処理し、LINE 内の会話で確認・修正しながら登録できる MVP を作る。

- 入口: LINE で画像送信
- 抽出: 既存 `ReceiptExtractionPipeline` を再利用
- 判定: 既存 `AUTO_ACCEPT / REVIEW_REQUIRED / REJECTED` をそのまま利用
- 会話: クイックリプライ中心で確定/修正/保留
- 集計: LINE コマンドで月次/年次合計を返す

---

## 2. MVP スコープ

### 実装する

- LINE Webhook 受信（署名検証込み）
- 画像メッセージの取得・一時保存
- `ocr_engine=documentai` で既存 pipeline 実行
- 抽出結果を SQLite に保存
- 会話セッション管理（修正・保留・確定）
- LINE Reply API / Push API 送信
- 集計コマンド（`今年の医療費`, `今月の医療費`, `未確認`, `ヘルプ`）

### 今回はやらない

- LLM による自由入力の高度解釈
- Web UI / LIFF
- 複雑明細（行明細の自動構造化）
- マルチテナント運用

---

## 3. 全体アーキテクチャ（現行整合版）

```text
LINE User
  -> LINE Messaging API (Webhook)
  -> app/line_webhook.py (FastAPI)
  -> linebot/webhook_handler.py
  -> linebot/media_client.py (message content download)
  -> app/pipeline.py ReceiptExtractionPipeline (documentai固定)
  -> inbox/repository.py (SQLite保存)
  -> inbox/conversation_service.py (状態遷移)
  -> linebot/reply_client.py (Reply/Push)
  -> LINE User
```

ポイント:

- 抽出ロジックは既存の `extractors/`, `resolver/`, `templates/` を流用
- `app/main.py` の CLI は残す（運用/検証用）
- LINE 会話層は新規モジュールとして分離し、既存 CLI への影響を最小化

---

## 4. 追加するモジュール構成（提案）

```text
app/
  line_webhook.py              # FastAPI entrypoint（新規）

linebot/
  webhook_handler.py           # イベント入口・署名検証後の振り分け（新規）
  media_client.py              # LINE画像取得APIクライアント（新規）
  reply_client.py              # LINE Reply/Push API クライアント（新規）
  message_templates.py         # 返信文面テンプレート（新規）
  quick_replies.py             # quick reply/postback data 生成（新規）
  signature.py                 # X-Line-Signature 検証（新規）

inbox/
  models.py                    # DB行モデル/DTO（新規）
  repository.py                # SQLite CRUD（新規）
  state_machine.py             # 会話状態遷移（新規）
  conversation_service.py      # 受信イベント処理と返信決定（新規）
  aggregate_service.py         # 集計コマンド処理（新規）
  retention.py                 # TTL削除処理（新規）

tests/
  test_line_signature.py
  test_line_webhook_handler.py
  test_inbox_state_machine.py
  test_inbox_repository.py
  test_conversation_service.py
  test_line_message_templates.py
```

既存モジュールの変更想定:

- `app/config.py`: LINE webhook/inbox 設定追加
- `config.example.yaml`: 新規設定例追加
- `requirements.txt`: `fastapi`, `uvicorn` 追加
- `.gitignore`: `data/inbox/images/` と SQLite ファイル追加

---

## 5. 設定仕様（追加）

`config.yaml` に以下を追加する。

```yaml
line_messaging:
  enabled: true
  channel_secret: ""
  channel_access_token: ""
  webhook_path: "/webhook/line"
  api_base_url: "https://api.line.me"
  data_api_base_url: "https://api-data.line.me"
  timeout_sec: 10
  allowed_user_ids: []

inbox:
  sqlite_path: "data/inbox/linebot.db"
  image_store_dir: "data/inbox/images"
  image_retention_days: 14
  session_ttl_minutes: 60
  max_candidate_options: 3
  enable_text_commands: true
```

Document AI 運用の前提:

```yaml
ocr:
  engine: documentai
  allowed_engines:
    - documentai
  engines:
    documentai:
      enabled: true
      project_id: "YOUR_GCP_PROJECT"
      location: "us"
      processor_id: "YOUR_PROCESSOR_ID"
      credentials_path: "C:\\path\\to\\service-account.json"
```

---

## 6. DB スキーマ（SQLite MVP）

### `receipts`

- `receipt_id` TEXT PK
- `line_user_id` TEXT NOT NULL
- `line_message_id` TEXT NOT NULL
- `image_path` TEXT NOT NULL
- `image_sha256` TEXT NOT NULL
- `document_id` TEXT
- `decision_status` TEXT NOT NULL
- `decision_confidence` REAL NOT NULL
- `processing_error` TEXT
- `created_at` TEXT NOT NULL
- `updated_at` TEXT NOT NULL

### `receipt_fields`

- `receipt_id` TEXT NOT NULL
- `field_name` TEXT NOT NULL
- `value_raw` TEXT
- `value_normalized` TEXT
- `score` REAL
- `ocr_confidence` REAL
- `reasons_json` TEXT
- `source` TEXT
- PK: (`receipt_id`, `field_name`)

### `conversation_sessions`

- `session_id` TEXT PK
- `line_user_id` TEXT NOT NULL
- `receipt_id` TEXT NOT NULL
- `state` TEXT NOT NULL
- `awaiting_field` TEXT
- `payload_json` TEXT
- `expires_at` TEXT NOT NULL
- `created_at` TEXT NOT NULL
- `updated_at` TEXT NOT NULL

### `aggregate_entries`

- `entry_id` TEXT PK
- `receipt_id` TEXT UNIQUE NOT NULL
- `line_user_id` TEXT NOT NULL
- `service_date` TEXT
- `provider_name` TEXT
- `amount_yen` INTEGER
- `family_member_name` TEXT
- `status` TEXT NOT NULL  # tentative / confirmed / hold
- `created_at` TEXT NOT NULL
- `updated_at` TEXT NOT NULL

### `processed_line_events`（冪等性）

- `event_id` TEXT PK
- `received_at` TEXT NOT NULL

---

## 7. 会話状態機械（MVP）

状態:

- `IDLE`
- `AWAIT_CONFIRM`
- `AWAIT_FIELD_SELECTION`
- `AWAIT_FIELD_CANDIDATE`
- `AWAIT_FREE_TEXT`
- `HOLD`
- `COMPLETED`

遷移の基本:

1. 画像受信
2. OCR + 抽出 + 判定
3. `AUTO_ACCEPT`: 登録して完了通知（修正導線を付与）
4. `REVIEW_REQUIRED`: 該当項目を順に確認
5. `REJECTED`: 再撮影案内

クイックリプライで扱う操作:

- `ok`（確定）
- `edit`（修正開始）
- `field:<field_name>`（修正対象選択）
- `pick:<index>`（候補選択）
- `free_text`（自由入力）
- `hold`（保留）
- `cancel`（キャンセル）
- `back`（1段戻る）

---

## 8. LINE メッセージ仕様（MVP）

### 画像受信直後

- 返信: `画像を受け付けました。読み取り中です。`
- 処理完了後:
  - `AUTO_ACCEPT`: `登録しました: 医療機関 / 日付 / 金額 / 対象者`
  - `REVIEW_REQUIRED`: `確認が必要です: 金額候補を選択してください`
  - `REJECTED`: `読み取りに失敗しました。明るい場所で再撮影してください`

### テキストコマンド

- `今年の医療費`
- `今月の医療費`
- `未確認`
- `ヘルプ`

`inbox/aggregate_service.py` で集計して返信。

---

## 9. 既存実装の流用方針

流用する:

- `ReceiptExtractionPipeline.process(...)`
- `ocr/documentai_adapter.py`
- `core/enums.py`, `core/models.py`
- `resolver/`, `extractors/`, `templates/`

新規実装する:

- LINE Webhook 受信・署名検証
- LINE 画像取得 API 呼び出し
- 会話ステート管理
- SQLite 永続化
- LINE Reply API / Push API クライアント

変更しない:

- 既存 CLI (`extract`, `batch`, `compare-ocr`, `learn-template`, `healthcheck-ocr`)
- 既存通知機能 (`notifications/`) の仕様

---

## 10. セキュリティ / 運用要件（MVP必須）

- `channel_secret`, `channel_access_token`, GCP 認証情報をリポジトリに置かない
- Webhook 署名検証は必須（失敗時 401）
- `line_user_id`, 金額, 日付, 氏名はログでマスク
- 画像は TTL で削除（`image_retention_days`）
- `processed_line_events` で webhook 冪等処理

---

## 11. 実装マイルストーン

### Milestone 1: 受信基盤

- `app/line_webhook.py` と署名検証
- 画像イベントを受け、メッセージIDから画像を保存
- webhook 冪等化

完了条件:

- LINE 画像送信でサーバーが 200 を返し、画像保存まで成功

### Milestone 2: OCR 連携と登録

- 保存画像を `ReceiptExtractionPipeline` へ接続（`documentai` 固定）
- 抽出結果を DB 保存
- `AUTO_ACCEPT / REVIEW_REQUIRED / REJECTED` に応じて返信

完了条件:

- 実画像で 3 判定すべて返信確認

### Milestone 3: 会話修正フロー

- `state_machine.py`, `conversation_service.py` 実装
- 候補選択、自由入力、保留、確定
- `aggregate_entries` 更新

完了条件:

- `REVIEW_REQUIRED` ケースが LINE 内で確定まで完走

### Milestone 4: 集計・運用

- テキストコマンド集計
- 画像 TTL 削除ジョブ
- ログマスキング最終確認

完了条件:

- `今年の医療費` / `今月の医療費` / `未確認` が正しく返る

---

## 12. テスト計画

単体テスト:

- 署名検証
- postback payload 解析
- 状態遷移
- repository CRUD
- 会話ハンドラ
- 集計計算

結合テスト:

- 画像イベント -> OCR -> 判定 -> 返信まで
- `REVIEW_REQUIRED` の修正完了フロー
- 冪等処理（同イベント再送）

回帰テスト:

- 既存 `tests/test_*.py` を維持し、CLI 機能が壊れていないことを確認

---

## 13. 受け入れ基準（MVP Done）

- LINE 画像送信から登録まで一連で動く
- `AUTO_ACCEPT / REVIEW_REQUIRED / REJECTED` が会話で扱える
- 修正・保留・確定が DB に反映される
- `今年の医療費` と `今月の医療費` が返る
- 秘密情報をコード/ログに露出しない

---

## 14. 不明点ゼロ化チェック

### 14.1 この計画で確定した事項

- OCR は `documentai` を第一優先で採用する
- 抽出ロジックは既存 pipeline を流用する
- 既存 CLI は残し、LINE は別入口として追加する
- データ保存は MVP では SQLite を採用する

### 14.2 実装前に最終確認が必要な事項

1. 実行基盤は `FastAPI + Uvicorn` で確定してよいか  
2. 画像保存先はローカル (`data/inbox/images`) で開始してよいか  
3. LINE 利用範囲は 1:1 チャット限定でよいか（グループ対応は後回し）  
4. `AUTO_ACCEPT` は「自動確定」運用でよいか（毎回確認はしない）  
5. `family_registry.required` は現行どおり `true` 維持でよいか  
6. 画像保持期間は 14 日でよいか  
7. 集計コマンドは `今年の医療費` / `今月の医療費` / `未確認` / `ヘルプ` で確定してよいか  

この 7 項目が確定したら「不明点ゼロ」とし、実装フェーズに進む。

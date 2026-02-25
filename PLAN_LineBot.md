# PLAN.md — 医療費領収書集計Bot（LINE起点 / ルールベース確認フロー MVP）

## 0. 目的

LINEで送られた医療費領収書画像をOCRで読み取り、  
**会話ベース（LINE内）で最小限の確認を行いながら** 年間医療費を集計できるMVPを作る。

- 入口UX: **LINEで写真を送るだけ**
- 確認UX: **原則LINE内で完結（クイックリプライ中心）**
- 軽量LLM: **現段階では使わない**（将来拡張）
- Web UI: **今回は必須にしない**（将来の逃げ道として設計だけ意識）

---

## 1. MVPのスコープ

### ✅ MVPでやること
- LINE Botで画像メッセージ受信
- 画像保存（短期）
- OCR実行（OCRアダプタは抽象化）
- 項目抽出（最低4項目）
  - 医療機関名
  - 日付
  - 金額
  - 対象者（本人/家族）
- ルールベースで判定
  - `AUTO_ACCEPT`
  - `REVIEW_REQUIRED`
  - `REJECTED`
- LINEの会話フローで確認/修正（クイックリプライ）
- DB保存（領収書、抽出結果、会話状態、集計）
- 年間集計の取得（LINEコマンド）

### ❌ MVPでやらないこと
- 軽量LLMによる自然言語理解
- Web確認UI（LIFF含む）
- 自動学習（ML再学習）
- 税務申告書類の出力
- 複雑な明細行解析（診療明細の行単位）

---

## 2. UX要件（最重要）

### UX原則
1. **入力はLINEで完結**（写真送信）
2. **確認は1ターン1判断**
3. **ボタン選択を優先**（自由入力は最終手段）
4. **怪しい時だけ確認**
5. **保留できる**
6. **仮登録を許す**（後で確定可能）

### メッセージ状態
- `AUTO_ACCEPT`
  - 例: `〇〇薬局 / 1,540円 / 2026-02-25 で登録しました`
  - クイックリプライ: `修正する`, `保留`
- `REVIEW_REQUIRED`
  - 例: `金額の候補を確認してください`
  - クイックリプライ: 候補値, `修正する`, `保留`
- `REJECTED`
  - 例: `読み取れませんでした。明るい場所で再撮影してください`
  - クイックリプライ: `撮り直すコツ`, `手入力する`（MVPでは手入力簡易対応でも可）

---

## 3. システム構成（MVP）

### 推奨構成（AWS想定）
- **LINE Webhook API**（FastAPI or Lambda）
- **画像保存**（S3）
- **OCR Worker**（同期でも可 / 将来非同期化しやすく）
- **DB**（SQLiteで開始 → 将来DynamoDB/RDS）
- **LINE Reply/Push API**
- **Secrets管理**（ローカル `.env` はダミーのみ、本番はSecrets Manager想定）

### アーキテクチャ方針
- OCRエンジンは **adapter interface** で抽象化
- 抽出ロジックは OCRと分離（`extractor`）
- 会話状態管理を独立（`conversation`）
- メッセージ文面はテンプレ管理（ハードコードしない）

---

## 4. リポジトリ構成（提案）

```text
app/
  main.py                       # FastAPI entry (webhook)
  config.py                     # 設定読み込み（env）
  logging.py                    # マスキング対応logger
  dependencies.py

core/
  models.py                     # dataclass / Pydantic models
  enums.py                      # Status/State enums
  errors.py
  time.py
  ids.py

linebot/
  webhook_handler.py            # LINEイベント入口
  reply.py                      # reply/push wrapper
  message_templates.py          # メッセージテンプレ
  quick_replies.py              # クイックリプライ生成

ocr/
  base.py                       # OCR adapter interface
  mock_adapter.py               # 開発用ダミー
  adapter_xxx.py                # 実OCR実装（後で差し替え）
  normalizer.py                 # OCR文字正規化（全角半角、円、日付）

extract/
  receipt_extractor.py          # OCR結果から候補抽出
  rule_engine.py                # AUTO_ACCEPT / REVIEW_REQUIRED / REJECTED 判定
  candidate_builder.py          # 候補生成（特に金額/日付/医療機関名）

conversation/
  state_machine.py              # 状態遷移
  handlers.py                   # ユーザー返信ごとの処理
  intents.py                    # ルールベース意図判定（OK/修正/保留等）
  session_store.py              # 会話セッション管理

domain/
  receipt_service.py            # 登録・修正・確定
  aggregate_service.py          # 年間集計
  user_profile_service.py       # 対象者（本人/家族）など

infra/
  storage/
    image_store.py              # 画像保存/取得/削除
  db/
    repositories.py             # CRUD
    schema.sql                  # 初期スキーマ（SQLite）
  queue/                        # 将来用（非同期化）
    jobs.py

tests/
  test_rule_engine.py
  test_extractor.py
  test_conversation_flow.py
  test_intents.py
  test_normalizer.py

scripts/
  seed_mock_data.py
  run_local.sh

docs/
  API.md
  MESSAGE_FLOW.md
  SECURITY.md
````

---

## 5. データモデル（MVP）

## 5.1 Receipt（領収書）

```python
Receipt:
  receipt_id: str
  user_id: str                  # LINE userId を内部IDに変換して保持
  image_uri: str                # 保存先（短期）
  image_sha256: str
  ocr_status: str               # pending / done / failed
  review_status: str            # auto_accepted / review_required / rejected / confirmed / hold
  created_at: datetime
  updated_at: datetime
```

## 5.2 ExtractedFields（抽出結果）

```python
ExtractedFields:
  receipt_id: str

  provider_name_value: str | None
  provider_name_confidence: float | None
  provider_name_candidates: list[str]
  provider_name_source_text: str | None

  amount_value: int | None
  amount_confidence: float | None
  amount_candidates: list[int]
  amount_source_text: str | None

  date_value: date | None
  date_confidence: float | None
  date_candidates: list[str]
  date_source_text: str | None

  person_value: str | None      # self / spouse / child / other
  person_confidence: float | None
  person_candidates: list[str]

  raw_ocr_text: str | None      # 長期保存しない方針（MVPでもTTLを意識）
```

## 5.3 ConversationSession（会話状態）

```python
ConversationSession:
  session_id: str
  user_id: str
  receipt_id: str
  state: str                    # enum (後述)
  awaiting_field: str | None    # amount/date/provider_name/person
  candidate_payload_json: str | None
  expires_at: datetime
  created_at: datetime
  updated_at: datetime
```

## 5.4 AggregateEntry（集計レコード）

```python
AggregateEntry:
  entry_id: str
  receipt_id: str
  user_id: str
  service_date: date
  provider_name: str
  amount_yen: int
  person: str
  status: str                   # tentative / confirmed / hold
  created_at: datetime
  updated_at: datetime
```

---

## 6. 状態遷移設計（ルールベース会話）

## 6.1 OCR判定状態（抽出後）

* `AUTO_ACCEPT`
* `REVIEW_REQUIRED`
* `REJECTED`

## 6.2 会話セッション状態（LINE返信処理）

* `IDLE`
* `AWAIT_CONFIRM`              # 「この内容で登録？」待ち
* `AWAIT_FIELD_SELECTION`      # 「どれを修正？」待ち
* `AWAIT_FIELD_CANDIDATE`      # 候補選択待ち
* `AWAIT_FREE_TEXT`            # 自由入力待ち（MVPは最小限）
* `COMPLETED`
* `HOLD`
* `CANCELLED`

## 6.3 基本フロー

1. 画像受信
2. OCR + 抽出 + 判定
3. 判定に応じてメッセージ送信
4. ユーザー返信（ボタン/テキスト）
5. セッション状態に応じて遷移
6. 確定 or 保留

---

## 7. ルールエンジン仕様（MVP）

## 7.1 判定ルール（初期案）

### `AUTO_ACCEPT`

以下を満たす場合

* 金額あり（候補1位）かつ confidence >= 0.9
* 日付あり confidence >= 0.8
* 医療機関名あり confidence >= 0.75
* 重大な矛盾なし（例：金額が0円、未来日付）

### `REVIEW_REQUIRED`

* 必須項目のいずれか confidence不足
* 金額候補が複数で近い（例: 4380 / 43800）
* 医療機関名候補に揺れがある
* 日付が曖昧（和暦/西暦変換不安定など）

### `REJECTED`

* 金額 or 日付が抽出不能
* OCRテキスト量が少なすぎる
* 画像品質が悪い（OCR文字数閾値未満など）

## 7.2 候補生成

### 金額候補

* `円` 周辺の数値を優先
* `合計`, `領収`, `今回` 近傍を優先
* 桁異常候補も保持（レビュー用）

### 日付候補

* `YYYY/MM/DD`, `YY.MM.DD`, `R6.2.25` 等を正規化
* 未来日付を除外（or 低優先）

### 医療機関名候補

* 上部の大きめ文字列（OCR依存）
* `医院`, `病院`, `クリニック`, `薬局`, `歯科` を含む文字列を優先
* 過去の確定済み医療機関名を優先候補に混ぜる（ユーザー辞書）

---

## 8. LINE会話フロー仕様（MVP）

## 8.1 ユーザー意図（ルールベース）

判定対象（完全一致 or 正規表現）:

* `OK` / `登録` / `はい`
* `修正する`
* `保留`
* `キャンセル`
* `金額`
* `日付`
* `医療機関名`
* `名前`
* 候補値（ボタンのpayloadを優先）
* 自由入力（フォールバック）

> 注意: MVPでは **ボタンpayload** を最優先に使い、自然言語解析を最小限にすること。

## 8.2 クイックリプライ設計

### 共通

* `OK`
* `修正する`
* `保留`
* `キャンセル`

### 修正項目選択

* `医療機関名`
* `金額`
* `日付`
* `名前`

### 候補選択

* 候補1
* 候補2
* 候補3（最大）
* `手入力`（MVP簡易）
* `戻る`

## 8.3 返信テンプレート（例）

* `AUTO_ACCEPT`: 登録完了＋修正導線
* `REVIEW_REQUIRED`: 怪しい項目を明示して確認依頼
* `REJECTED`: 再撮影ガイド
* 修正完了: `金額を 4,380円 に修正しました`
* 最終確認: `この内容で確定しますか？`
* 完了: `登録しました（仮登録/確定）`

---

## 9. コマンド仕様（LINEテキスト）

MVPで最低限サポート

* `今年の医療費`

  * 年間合計と件数を返す
* `今月の医療費`

  * 月間合計と件数
* `未確認`

  * `REVIEW_REQUIRED` / `HOLD` 件数
* `ヘルプ`

  * 使い方

将来

* `一覧`
* `家族別`
* `修正 #ID`
* `CSV出力`

---

## 10. セキュリティ・運用要件（MVP必須）

## 10.1 秘密情報管理

* APIキー/チャネルシークレットは **repoに置かない**
* `.env.example` のみコミット
* 本番は Secrets Manager（または同等）
* `config.py` で必須値検証（不足時は起動失敗）

## 10.2 Secret誤コミット対策（必須）

* `pre-commit` 導入

  * `gitleaks` or `detect-secrets`
* CIでも secret scan 実行
* `.gitignore` に以下含める

  * `.env`
  * `*.pem`
  * `*.key`
  * `secrets.*`

## 10.3 ログ設計

* OCR全文・画像URL・個人情報を `INFO` ログに出さない
* 例外ログも request body 全出し禁止
* マスキング対象

  * 医療機関名（部分マスク可）
  * 金額
  * 日付
  * LINE userId
* 監査用イベントログは別（最小限）

## 10.4 データ保持

* 画像は短期保存（TTL削除）

  * MVP目安: 7〜30日
* 構造化データは保持
* `raw_ocr_text` は短期保存 or 無効化可能設計

## 10.5 権限

* DB/Storageアクセス権は最小権限
* 開発環境と本番環境の分離

---

## 11. 実装マイルストーン

## Milestone 1: 基盤（ローカルで動く）

### 目標

画像受信イベントをモックで通し、OCRモック結果から会話返信できる。

### タスク

* [ ] FastAPI webhook 雛形
* [ ] LINE署名検証
* [ ] `core/models.py` 作成
* [ ] `ocr/base.py`, `ocr/mock_adapter.py`
* [ ] `extract/receipt_extractor.py`（モックOCR文字列対応）
* [ ] `extract/rule_engine.py`
* [ ] `linebot/message_templates.py`
* [ ] `conversation/state_machine.py`
* [ ] SQLiteスキーマ作成
* [ ] ローカルE2E（モック）

### 完了条件

* モック画像イベント → `AUTO_ACCEPT/REVIEW_REQUIRED/REJECTED` の返信が出る

---

## Milestone 2: ルールベース確認フロー

### 目標

LINE内のクイックリプライだけで修正・確定まで完結。

### タスク

* [ ] クイックリプライ payload設計
* [ ] `conversation/handlers.py` 実装
* [ ] `intents.py`（ルールベース）
* [ ] 修正項目選択フロー
* [ ] 候補選択フロー（amount/date/provider/person）
* [ ] 保留/キャンセル/戻る
* [ ] セッション期限切れ処理
* [ ] 会話フローの単体テスト

### 完了条件

* `REVIEW_REQUIRED` のケースが LINE内で確定まで完走する

---

## Milestone 3: 実OCR統合（1つ）

### 目標

OCRアダプタを1つ実装して実画像で検証可能にする。

### タスク

* [ ] 実OCR adapter 実装（任意の1サービス）
* [ ] 画像保存（S3またはローカル代替）
* [ ] `ocr/normalizer.py` 強化（全角/半角、円、和暦）
* [ ] 候補生成ロジック改善
* [ ] 失敗時の `REJECTED` ガイド整備
* [ ] 10〜30枚で精度検証（家族データ）

### 完了条件

* 家族データで主要ケースが動作
* 誤認識時も会話修正で回復できる

---

## Milestone 4: 集計機能と運用基盤

### 目標

実利用できる最低限の集計・セキュリティ運用を整える。

### タスク

* [ ] `今年の医療費` / `今月の医療費` コマンド
* [ ] 仮登録/確定フラグ
* [ ] Secret scan（pre-commit + CI）
* [ ] ログマスキング
* [ ] 画像TTL削除ジョブ（簡易で可）
* [ ] 削除API/スクリプト（最低限）

### 完了条件

* 家族向けクローズド運用開始可能

---

## 12. テスト方針

## 12.1 単体テスト（必須）

* `normalizer`
* `rule_engine`
* `candidate_builder`
* `intents`
* `state_machine`

## 12.2 会話E2Eテスト（必須）

最低限以下のシナリオを自動化

1. `AUTO_ACCEPT` → 完了
2. `REVIEW_REQUIRED` → `OK` で完了
3. `REVIEW_REQUIRED` → `修正する` → `金額` → 候補選択 → 完了
4. `REJECTED` → 再撮影案内
5. `保留` → `未確認`コマンドで確認できる

## 12.3 実データ評価（手動で可）

* 家族協力の領収書をカテゴリ別に収集

  * 病院 / 薬局 / 歯科
* 評価項目

  * 金額抽出成功率
  * 日付抽出成功率
  * 医療機関名抽出成功率
  * 修正所要時間（1件あたり）

---

## 13. 受け入れ基準（MVP Done）

### 機能

* [ ] LINE画像送信で処理開始できる
* [ ] `AUTO_ACCEPT/REVIEW_REQUIRED/REJECTED` が動く
* [ ] `REVIEW_REQUIRED` をLINE内会話で修正・確定できる
* [ ] 年間集計コマンドが返る

### UX

* [ ] 修正フローが「1ターン1判断」になっている
* [ ] ボタン中心で完結する（自由入力は例外）
* [ ] 保留できる

### 安全性

* [ ] Secret誤コミット対策が有効
* [ ] ログに個人情報を出さない
* [ ] 画像TTL削除の仕組みがある

---

## 14. 将来拡張（MVP後）

* 軽量LLMで自然言語修正の意図分類/スロット抽出
* Web確認UI（詰みケースの救済）
* 医療機関名辞書学習の強化
* 複数領収書の一括確認
* CSV/年次エクスポート
* 家族アカウントの管理強化
* OCR二段構え（軽量→高精度）

---

## 15. Codexへの実装指示（重要）

1. **まずはルールベースを優先**し、自然言語理解の高度化は行わないこと。
2. **会話状態管理（state machine）を先に固定**すること。
3. OCRは `ocr/base.py` のインターフェースに従い、最初は `mock_adapter.py` で動作確認すること。
4. 返信文面は `message_templates.py` に集約し、ハードコードを分散させないこと。
5. 秘密情報はコード・repoに埋め込まないこと。`.env.example` のみコミット可。
6. ログはマスキング前提で実装すること（デバッグで個人情報を出さない）。
7. 小さく動くE2E（モック）を最初に通してからOCR統合に進むこと。

---

## 16. 最初に作るファイル（優先順）

* [ ] `core/enums.py`
* [ ] `core/models.py`
* [ ] `ocr/base.py`
* [ ] `ocr/mock_adapter.py`
* [ ] `ocr/normalizer.py`
* [ ] `extract/receipt_extractor.py`
* [ ] `extract/rule_engine.py`
* [ ] `linebot/message_templates.py`
* [ ] `linebot/quick_replies.py`
* [ ] `conversation/state_machine.py`
* [ ] `conversation/intents.py`
* [ ] `conversation/handlers.py`
* [ ] `domain/receipt_service.py`
* [ ] `infra/db/schema.sql`
* [ ] `app/main.py`

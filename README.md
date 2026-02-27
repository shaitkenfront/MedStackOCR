# MedStackOCR MVP

医療費控除向けの領収書OCR・整理システムです。  
このブランチは **LINE Webhook + AWS Lambda 本番運用専用** です。

## 概要
- LINEで受けた領収書画像を Google Document AI でOCR
- 抽出項目: 医療機関 / 日付 / 金額 / 対象者
- 判定: `AUTO_ACCEPT` / `REVIEW_REQUIRED` / `REJECTED`
- 支払日年チェック: システム日付基準で「当年または前年」を許容
- 会話型修正フローと累計医療費集計を提供

## 構成
- API Gateway (HTTP API)
- Lambda (Ingress / Worker の2段)
- SQS FIFO
- DynamoDB
- S3（原本画像）
- Google Document AI（OCR）

主要実装:
- `app/lambda_handlers/ingress_handler.py`
- `app/lambda_handlers/worker_handler.py`
- `infra/cdk/`

## 主な機能
- 家族氏名登録（LINEユーザー単位、alias対応）
- OCR結果の確認・修正・確定
- 重複候補検知（画像ハッシュ / 項目一致）
- 訂正履歴に基づく学習ヒント
- 控除対象外の可能性キーワード検知（注意喚起）
- ブロック時（`unfollow`）のユーザーデータ削除

## いたずら利用対策
- OCR実行前に同一画像ハッシュをチェック
- OCR実行レート制限（既定値）
  - ユーザー: `3件/分`
  - ユーザー: `40件/日`
  - 全体: `1200件/日`
- 制限超過時はDocAIを呼ばず、LINEに案内メッセージを返却

## 設定
- 機密値（LINEトークン / DocAI認証情報）は Secrets Manager で管理します。
- 非機密値は CDK context と Lambda環境変数で管理します。

## デプロイ
- デプロイ手順は `infra/cdk/README.md` を参照してください。

## LINE操作
テキストコマンド:
- `今年の医療費`
- `今月の医療費`
- `未確認`
- `ヘルプ`
- `取り消し`（類義語: 取消 / やり直し / 削除 / 失敗）

## 初回オンボーディング
- `follow` イベントで家族氏名登録を開始
- ステップ入力: 氏名 -> ヨミガナ -> 誤表記（任意）
- クイックリプライ: `次の名前の入力に進む` / `名前登録を終了する`
- 登録完了まで通常OCR処理に進まない

## テスト
```bash
python -m unittest discover -s tests -p "test_*.py"
```

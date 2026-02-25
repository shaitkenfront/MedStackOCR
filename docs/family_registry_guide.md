# family_registry 運用手順

## 1. 事前準備
- `family_registry.template.yaml` を開く。
- `canonical_name` は正規表記（例: `山田 太郎`）で統一する。
- `aliases` には OCR の誤認識や表記ゆれを登録する。

## 2. 登録ルール
- すべての家族を `members` に登録する（必須）。
- `aliases` には次を優先して追加する。
  - スペース有無（`山田太郎` / `山田 太郎`）
  - 敬称付き（`山田 太郎様`）
  - カナ表記（`ヤマダ タロウ`）
  - よく出る誤読（例: `ヤマダ` -> `ヤマタ`）

## 3. 設定反映
- `config.yaml` の `family_registry` をテンプレート内容で更新する。
- `required: true` のまま運用する。

## 4. 判定仕様（現行）
- 辞書一致（`canonical_name` または `aliases` 一致）: 通常判定
- 未登録だが同姓: `REVIEW_REQUIRED`
- 未登録かつ異姓: `REJECTED`

## 5. 日次運用フロー
1. Webhook起動: `uvicorn app.line_webhook:app --host 0.0.0.0 --port 8000`
2. LINEで領収書画像を送信
3. `REVIEW_REQUIRED` / `REJECTED` の `family_member_name` と `decision.reasons` を確認
4. 正当な家族なら `aliases` を追加して再送信
5. 他姓ノイズなら辞書追加せずそのまま `REJECTED` を維持

## 6. メンテナンスの目安
- 1名につき最低3〜5個の `aliases` を持つ
- 新しいOCRエンジン・帳票形式を追加したら aliases を再点検する
- 誤検出が続く場合は `aliases` 追加を優先し、抽出ロジック変更は最後に検討する

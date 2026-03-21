# 履歴ページ 編集・削除・画像追加機能 開発プラン

作成日: 2026-03-21

---

## 事前決定事項

### 物理削除 vs 論理削除

**物理削除を採用する。**

| 観点 | 物理削除 | 論理削除 |
|------|---------|---------|
| 実装コスト | 低（削除クエリのみ） | 高（全クエリに`WHERE deleted_at IS NULL`追加が必要） |
| 誤削除防護 | 確認ダイアログで十分 | DB復元可能だがUIからは不可 |
| 影響範囲 | `get_history` `get_stats` `get_report_data`に変更不要 | すべてのクエリを修正必要 |
| 本アプリ特性 | シングルユーザー・監査ログ不要 | マルチユーザー・監査用途に向く |

→ 確認ダイアログ付きの物理削除で十分安全。

---

## 追加・変更ファイル一覧

| ファイル | 変更内容 |
|---------|---------|
| `database.py` | CRUD関数追加（更新・削除・画像リスト取得） |
| `main.py` | APIエンドポイント追加 |
| `static/history.html` | 編集・削除・画像UIの全面追加 |

---

## Step 1 — database.py 関数追加

### 食事記録

- `update_meal_full(meal_id, meal_date, meal_type, description, calories, protein, fat, carbs, sodium, notes)`
  - meal_dateを含む全フィールド更新
- `delete_meal(meal_id)` — 物理削除

### 体重記録

- `get_history()` のweightクエリに `id` カラムを追加してレスポンスに含める
- `update_weight_by_id(weight_id, weight_kg)` — weight_kgのみ更新可
- `delete_weight_by_id(weight_id)` — 物理削除

### 歩数記録

- `get_history()` のstepsクエリに `id` カラムを追加してレスポンスに含める
- `update_steps_by_id(steps_id, steps)` — steps値のみ更新可
- `delete_steps_by_id(steps_id)` — 物理削除

### 食事画像

- `get_meal_images(meal_id)` — `[{id, mime_type}, ...]` を返す（全件）
- `delete_meal_image(image_id)` — 物理削除

---

## Step 2 — main.py APIエンドポイント追加

```
PUT    /api/meals/{meal_id}            # 食事記録更新
DELETE /api/meals/{meal_id}            # 食事記録削除

PUT    /api/weight/{weight_id}         # 体重更新（weight_kgのみ）
DELETE /api/weight/{weight_id}         # 体重削除

PUT    /api/steps/{steps_id}           # 歩数更新（stepsのみ）
DELETE /api/steps/{steps_id}           # 歩数削除

GET    /api/meals/{meal_id}/images     # 食事の画像一覧（id付き）
POST   /api/meals/{meal_id}/images     # 食事に画像追加（base64受け取り・image_utils経由）
DELETE /api/images/{image_id}          # 画像削除
```

---

## Step 3 — history.html UI追加

### 3-1. 食事カードのボタン追加

各 `.meal-card` に以下を追加：

- ✏️ 編集ボタン → 食事編集モーダルを開く
- 🗑️ 削除ボタン → 確認ダイアログ → 物理削除 → 再描画
- 📷 写真追加ボタン → `<input type="file" accept="image/*">` を隠して呼び出し → base64変換 → `POST /api/meals/{id}/images`
- 既存の画像表示部分：各画像に🗑️削除ボタンを追加

### 3-2. 体重・歩数のボタン追加

`.day-meta` の体重・歩数表示にも同様に ✏️ と 🗑️ を追加

### 3-3. 食事編集モーダル仕様

| フィールド | 入力型 | バリデーション |
|-----------|-------|--------------|
| 食事日 (meal_date) | `<input type="date">` | 必須 |
| 食事区分 (meal_type) | `<select>` | 朝食/昼食/夕食/間食/夜食 から選択必須 |
| 内容 (description) | `<input type="text">` | 必須、最大200文字 |
| カロリー (calories) | `<input type="number">` | 整数、0〜9999 kcal |
| タンパク質 (protein) | `<input type="number">` | 小数点1位、0〜999.9 g |
| 脂質 (fat) | `<input type="number">` | 小数点1位、0〜999.9 g |
| 炭水化物 (carbs) | `<input type="number">` | 小数点1位、0〜999.9 g |
| 食塩相当量 (sodium) | `<input type="number">` | 小数点2位、0〜99.99 g |
| メモ (notes) | `<input type="text">` | 任意、最大200文字 |

### 3-4. 体重編集モーダル仕様

| フィールド | 入力型 | バリデーション |
|-----------|-------|--------------|
| 日付 | 表示のみ（変更不可） | — |
| 時間帯 | 表示のみ（変更不可） | — |
| 体重 (weight_kg) | `<input type="number">` | 小数点1位、20.0〜200.0 kg、必須 |

### 3-5. 歩数編集モーダル仕様

| フィールド | 入力型 | バリデーション |
|-----------|-------|--------------|
| 日付 | 表示のみ（変更不可） | — |
| 歩数 (steps) | `<input type="number">` | 整数、0〜99999、必須 |

### 3-6. モーダル共通仕様

- オーバーレイ外タップ・クリックでは閉じない
- 「保存」または「キャンセル」ボタンのみで閉じる
- 保存前にバリデーション。エラー時はフィールド下に赤字メッセージ表示
- 保存成功時は自動でページ再読み込み（現在の期間設定を維持）

---

## 実装順序

1. `database.py` — 関数追加・historyクエリ修正
2. `main.py` — エンドポイント追加
3. `static/history.html` — UI実装（モーダルCSS → レンダリング関数 → API連携）

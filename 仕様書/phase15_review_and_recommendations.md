# Phase 15 レビュー：日の区切り設定 & AI サマリーバグ修正

## レビュー対象

- 開発プラン: `仕様書/phase15_development_plan.md`
- コミット: `f4e59c4`〜`9f3e636`（6コミット）
- 変更ファイル: `database.py`, `main.py`, `claude_client.py`, `static/settings.html`
- テスト: `test/test_phase15.py`（17件 ALL PASSED / .gitignore対象）

---

## C: コードレビュー

### 評点: 7.5 / 10

Phase 15 の中核ロジック（`get_logical_today_jst()`、`_classify_weight_record` パラメータ化、`build_system_prompt` 論理日付化、設定キー3箇所追加）は正確に実装されている。テストも CLAUDE.md 規約（tempfile DB 隔離、datetime.now モック、境界値テスト）を遵守しており品質は高い。ただし、`day_start_hour` の適用範囲に**漏れが2箇所**あり、論理日付の一貫性が完全ではない。

---

### C-1 [P1] `_tool_record_meal` が `datetime.now(JST)` を使用 — 論理日付の一貫性欠落

**ファイル:** `claude_client.py` line 497, 500

**問題:**
```python
def _tool_record_meal(inp: dict) -> dict:
    meal_time = inp.get("meal_time")
    if meal_time is None:
        today_str = datetime.now(JST).strftime("%Y-%m-%d")  # ← 暦の今日
        meal_date = inp.get("meal_date", today_str)         # ← フォールバック値が不整合
        if meal_date == today_str:                           # ← 暦の今日と比較
            meal_time = datetime.now(JST).strftime("%H:%M")
```

Claude が `meal_date` を省略した場合のフォールバック値が「暦の今日」であり、Block 2 でClaudeに伝えている「論理的な今日」と不整合になる。また、`meal_time` の自動設定判定も暦の今日との比較であるため、深夜（0:00〜day_start_hour）に論理日付で記録した食事の `meal_time` が `None` になる。

**影響:** Claude がシステムプロンプト通りに `meal_date` を論理日付で指定する限り実害は低い。ただし、Claude が `meal_date` を省略するケース（仕様上は起こらないが、AI の挙動として完全には排除できない）では日付のズレが発生する。

**修正指示:**

```python
def _tool_record_meal(inp: dict) -> dict:
    meal_time = inp.get("meal_time")
    if meal_time is None:
        today_str = database.get_logical_today_jst()
        meal_date = inp.get("meal_date", today_str)
        if meal_date == today_str:
            meal_time = datetime.now(JST).strftime("%H:%M")
```

`meal_time` は実際の壁時計時刻でよいため `datetime.now(JST)` のまま（「何時に食べたか」は論理日付と無関係）。変更するのは `today_str`（日付判定）のみ。

---

### C-2 [P2] `build_system_prompt` の `int()` 変換に防御がない

**ファイル:** `claude_client.py` line 451

**問題:**
```python
day_start_hour = int(database.get_setting("day_start_hour") or "4")
```

`_select_best_weight_records` には `try/except ValueError` + `logger.warning` のフォールバックがあるが、`build_system_prompt` にはない。DB 値が不正な場合（直接 DB 操作等）、プロンプト生成が例外で失敗し、チャット全体が動作しなくなる。

**修正指示:**

```python
raw_hour = database.get_setting("day_start_hour") or "4"
try:
    day_start_hour = int(raw_hour)
except ValueError:
    logger.warning("day_start_hour の設定値が不正です: %s。デフォルト値 4 を使用します。", raw_hour)
    day_start_hour = 4
```

---

### C-3 [P2] `hour_end = day_start_hour - 1` — `day_start_hour=0` で負値

**ファイル:** `claude_client.py` line 395

**問題:** `day_start_hour=0` のとき `hour_end = -1` となり、プロンプトに以下が出力される：

```
日の区切りは午前0時。0:00〜-1:59 の記録は前日として扱う
```

この指示は意味不明。動作上は `day_start_hour=0` は「境界なし（全時間が当日扱い）」であり正しく動作するが、Claude へのプロンプトが誤解を招く。

**修正指示:**

`_build_block1_text` 内で条件分岐を追加する：

```python
    hour_end = day_start_hour - 1
    if day_start_hour > 0:
        day_boundary_rule = (
            f"- 日の区切りは午前{day_start_hour}時。0:00〜{hour_end}:59 の記録は前日として扱う\n"
        )
    else:
        day_boundary_rule = ""
```

ルール行を `{day_boundary_rule}` として埋め込む。`day_start_hour=0` のときはルール自体を出力しない（境界なしなので不要）。

---

### C-4 [P3] `get_previous_weight` のデフォルト引数に `today_jst()` が残存

**ファイル:** `database.py` line 584

**問題:**
```python
def get_previous_weight(time_of_day: str, before_date: Optional[str] = None) -> Optional[float]:
    before_date = before_date or today_jst()
```

現在の唯一の呼び出し元（`_tool_record_weight`）は `log_date` を明示するため、このデフォルトは使用されない。ただし、将来の呼び出し元がデフォルトに依存した場合に不整合が発生する。

**修正指示:**

```python
    before_date = before_date or get_logical_today_jst()
```

---

### C-5 [P3] `get_history()`, `get_stats()`, `get_frequent_meals()` が暦の日付を使用

**ファイル:** `database.py` line 828, 903, 423

**問題:** これらの関数は `datetime.now(JST).date()` で「直近N日」の起点を決定しているが、`day_start_hour` を考慮していない。

**影響:** 深夜 0:00〜day_start_hour に履歴・統計画面を開いた場合、「直近30日」の範囲が1日ずれる。ただし、これらは期間表示用でありデータの記録先日付には影響しないため、体感上の影響は微小。

**修正指示:**

3箇所とも同じパターン:
```python
# 変更前
today = datetime.now(JST).date()
# 変更後
today = _date.fromisoformat(get_logical_today_jst())
```

`_date` は `database.py` 冒頭で `from datetime import ..., date as _date` としてインポート済み。

---

### C-6 [良好] テスト品質

- tempfile DB 隔離、`gc.collect()` での Windows WAL ロック解放
- `unittest.mock.patch("database.datetime")` による時刻モック — `timedelta` 等の非モック対象を壊さない手法
- `_classify_weight_record` を純粋関数として直接テスト（DB不要）
- API テストは `TestClient` + ログイン済みフィクスチャ
- `day_start_hour=17` による一意値テスト（誤検知を防ぐ設計）
- 境界値テスト（0, 4, 23）

---

### C-7 [良好] _select_best_weight_records の ValueError フォールバック

Phase 10 テストとの互換性を維持しつつ、`logger.warning` 付きフォールバックを追加。サイレント障害禁止規約を遵守している。

---

### C-8 [良好] 設定キー3箇所チェック完了

CLAUDE.md メモリの「設定キー追加時の必須3箇所チェックリスト」を遵守：
- `database.py`: `init_db()` にデフォルト値 `("day_start_hour", "4")`
- `main.py`: `EDITABLE_SETTINGS` + `plain_keys` に追加、バリデーション追加
- `settings.html`: UI + `saveSettings()` に追加

---

## D: デザインレビュー

### 評点: 8.0 / 10

設定項目の追加は UI デザインの観点では小規模であり、既存の「目標」セクション内に自然に収まっている。大きな問題はないが、ラベルの分かりやすさに改善の余地がある。

---

### D-1 [P3] 「日の区切り時間」ラベルの分かりやすさ

**問題:** 「日の区切り時間」は技術的な概念を直接表現しており、初見のユーザーには意味が伝わりにくい。説明テキスト（「例: 4 に設定すると...」）があるため致命的ではないが、ラベル自体で直感的に理解できることが望ましい。

**修正指示:**

ラベルを「1日の開始時刻」に変更する：

```html
<span class="row-label">1日の開始時刻</span>
```

説明テキストも合わせて調整：
```
例: 4 に設定すると、0:00〜3:59 に記録した食事や体重は前日分として扱います
```

---

### D-2 [P3] 説明行のインラインスタイル

**問題:** 説明行（「例: 4 に設定すると...」）に `style="padding:6px 14px;border-bottom:1px solid var(--border)"` がインラインで付与されている。`.row` クラスの CSS で既に `border-bottom: 1px solid var(--border)` が設定されているため、`border-bottom` は冗長。

**修正指示:**

冗長な `border-bottom` を削除：
```html
<div class="row" style="padding:6px 14px">
```

---

## U: ユーザーレビュー

### 評点: 8.5 / 10

報告されたバグ（前日の朝食が翌日サマリーに混入）に対する修正は的確。`get_daily_summary` 必須化ルールをシステムプロンプトに追加する手法は、LLM の会話履歴依存による推測を防ぐ実用的なアプローチ。設定画面からの日の区切り時間変更もユーザーの要件に合致している。

---

### U-1 [P3] 設定変更の影響に関する説明不足

**問題:** `day_start_hour` を変更した場合、体重記録の分類（`_classify_weight_record`）や日次サマリー（`get_daily_summary`）の返却データが変わる可能性がある。ユーザーが設定を頻繁に変更すると、過去のデータの見え方が変わるが、この影響が説明されていない。

**修正指示:**

settings.html の説明テキストに以下を追記：

```html
<span style="font-size:12px;color:var(--text-secondary)">
  例: 4 に設定すると 0:00〜3:59 の記録は前日扱いになります（深夜の夕食を前日として集計）。
  変更しても過去に記録済みの食事データの日付は変わりません。
</span>
```

---

### U-2 [良好] バグの根本修正

会話履歴からの日次サマリー推測を禁止し、`get_daily_summary` ツール呼び出しを必須化するアプローチは正しい。Claude が DB の正確なデータに基づいてサマリーを構築するようになるため、日をまたいだ食事記録の混入が防止される。

---

## 修正優先度まとめ

| # | 優先度 | 区分 | 内容 |
|---|--------|------|------|
| C-1 | P1 | Code | `_tool_record_meal` の `today_str` を `get_logical_today_jst()` に変更 |
| C-2 | P2 | Code | `build_system_prompt` の `int()` 変換に try/except フォールバック追加 |
| C-3 | P2 | Code | `day_start_hour=0` 時のプロンプト文言修正（条件分岐） |
| C-4 | P3 | Code | `get_previous_weight` のデフォルト引数を `get_logical_today_jst()` に変更 |
| C-5 | P3 | Code | `get_history`, `get_stats`, `get_frequent_meals` を論理日付に変更 |
| D-1 | P3 | Design | ラベルを「1日の開始時刻」に変更 |
| D-2 | P3 | Design | 説明行の冗長なインラインスタイル削除 |
| U-1 | P3 | UX | 設定変更時の影響に関する説明追記 |

**P1（必須修正）: 1件**  
**P2（推奨修正）: 2件**  
**P3（改善提案）: 5件**

---

## レビュアー所感

Phase 15 は、ユーザーから報告された実バグに起因する改修であり、問題の根本原因分析（Claude の会話履歴依存）と解決策（`get_daily_summary` 必須化 + 論理日付統一）は的確である。設定キー追加のワークフロー（DB→API→UI の3箇所同時更新）も規約通りに実施されている。

ただし、`day_start_hour` という概念を導入しながら、その適用範囲に漏れがある点が減点対象となった。特に C-1（`_tool_record_meal`）は、Phase 15 の変更動機である「日の区切りの一貫性」に直結する箇所であり、見落としとしては痛い。開発プランの設計フェーズで `datetime.now(JST)` の全出現箇所を grep し、影響範囲を網羅的に洗い出すステップを入れるべきだった。

P1（C-1）を修正すれば、日の区切りに関する一貫性は実用上十分なレベルに達する。

---

## レビュー指摘修正実施結果

**修正日:** 2026-04-01  
**修正コミット:** `c0a8cbd`（Phase15-ReviewFix）  
**修正ファイル:** `claude_client.py`, `database.py`, `static/settings.html`

### 修正内容

| # | 優先度 | 修正内容 | 対応 |
|---|--------|---------|------|
| C-1 | P1 | `_tool_record_meal` の `today_str` を `database.get_logical_today_jst()` に変更 | ✅ 修正済 |
| C-2 | P2 | `build_system_prompt` の `int()` 変換に `try/except ValueError` + `logger.warning` を追加 | ✅ 修正済 |
| C-3 | P2 | `_build_block1_text` に `day_start_hour > 0` の条件分岐を追加し、`day_start_hour=0` 時は境界ルール行を出力しない | ✅ 修正済 |
| C-4 | P3 | `get_previous_weight` のデフォルト引数を `today_jst()` → `get_logical_today_jst()` に変更 | ✅ 修正済 |
| C-5 | P3 | `get_history`（line 828）、`get_stats`（line 903）、`get_frequent_meals`（line 423）の `datetime.now(JST).date()` を `_date.fromisoformat(get_logical_today_jst())` に変更 | ✅ 修正済 |
| D-1 | P3 | ラベルを「日の区切り時間」→「1日の開始時刻」に変更。バリデーションエラーメッセージも同様に更新 | ✅ 修正済 |
| D-2 | P3 | 説明行のインラインスタイルから冗長な `border-bottom:1px solid var(--border)` を削除 | ✅ 修正済 |
| U-1 | P3 | 説明テキストを更新（「例: 4 に設定すると、0:00〜3:59 に記録した食事や体重は前日分として扱います。変更しても過去に記録済みの食事データの日付は変わりません。」）| ✅ 修正済 |

### テスト結果

```
test/test_phase15.py: 17 passed in 1.67s（ALL PASSED）
全スイート: 809 passed, 1 failed（既存障害: test_phase12_step2.py::TestAddMealImageEndpoint::test_upload_file_exists_on_disk — Phase 15 以前から存在・Phase 15 変更と無関係）
```

---

## 既存障害テスト修正結果

**修正日:** 2026-04-01  
**修正ファイル:** `test/test_phase12_step2.py`（`.gitignore` 対象のためコミット外）

### 原因

`test_upload_file_exists_on_disk` が失敗していた原因：

- テストは `main.UPLOAD_DIR` を一時ディレクトリにパッチしていたが、実際のファイル保存処理は `image_utils.save_image_to_fs()` 内の `_UPLOAD_DIR`（モジュール変数）を使用する
- `image_utils._UPLOAD_DIR` はパッチされていなかったため、ファイルは本物の `uploads/meal_images/` に書き込まれ、テストの一時ディレクトリには存在しない状態になっていた

### 修正内容

`TestAddMealImageEndpoint.setUp` に `patch.object(image_utils, "_UPLOAD_DIR", upload_dir)` を追加し、`tearDown` で `stop()` を呼ぶよう修正。

```python
# 追加したパッチ
self.patch_image_utils = patch.object(image_utils, "_UPLOAD_DIR", upload_dir)
self.patch_image_utils.start()
# tearDown に追加
self.patch_image_utils.stop()
```

### テスト結果

```
test/test_phase12_step2.py: 22 passed in 1.65s（ALL PASSED）
全スイート: 810 passed, 0 failed（完全クリア）
```

---

## 再レビュー（修正後）

**レビュー日:** 2026-04-01  
**レビュー対象コミット:** `c0a8cbd`（Phase15-ReviewFix）  
**テスト修正:** `test/test_phase12_step2.py`（.gitignore 対象・コミット外）

---

### C: コードレビュー（再評価）

### 再評点: 9.0 / 10（前回 7.5）

全8件の指摘事項が正確に修正されている。論理日付の一貫性が大幅に改善された。

#### 修正確認

| # | 判定 | 詳細 |
|---|------|------|
| C-1 | ✅ 正確 | `_tool_record_meal` line 507: `database.get_logical_today_jst()` に変更済。`meal_time` は壁時計時刻のまま `datetime.now(JST)` で正しい（「何時に食べたか」は論理日付と無関係）|
| C-2 | ✅ 正確 | `build_system_prompt` line 460-465: `try/except ValueError` + `logger.warning` + デフォルト `4` フォールバック。CLAUDE.md のサイレント障害禁止規約を遵守 |
| C-3 | ✅ 正確 | `_build_block1_text` line 395-401: `day_start_hour > 0` で条件分岐し、`day_start_hour=0` 時は空文字列を出力。テンプレート内の埋め込みも `{day_boundary_rule}` に置換済 |
| C-4 | ✅ 正確 | `get_previous_weight` line 584: `get_logical_today_jst()` に変更済 |
| C-5 | ✅ 正確 | 3箇所とも `_date.fromisoformat(get_logical_today_jst())` に統一。型も `date` オブジェクトで後続の `timedelta` 演算と整合 |

#### `datetime.now(JST)` 残存確認

修正後の全出現を `grep` で検証した結果、残存は以下の2箇所のみ：

| ファイル | 行 | 用途 | 判定 |
|---------|-----|------|------|
| `database.py:15` | `today_jst()` 関数定義 | 旧関数。下記参照 | ⚠️ 後述 |
| `database.py:273` | `get_logical_today_jst()` 内部 | 現在時刻の取得（`now.hour < hour` 判定用）| ✅ 正当な使用 |
| `claude_client.py:510` | `_tool_record_meal` 内 | `meal_time`（食事時刻）の取得 | ✅ 正当な使用（壁時計時刻）|

いずれも「日付の決定」ではなく「時刻の取得」目的であり、論理日付との不整合は生じない。

#### C-9 [情報] `today_jst()` が未使用関数として残存

`database.py:13` に定義されている `today_jst()` は、Phase 15 修正完了後にどこからも呼び出されていない（grep で確認済）。削除して差し支えないが、外部スクリプト等が参照している可能性もあるため、情報提供に留める。削除するかどうかは開発者判断。

#### C-10 [P3] `get_logical_today_jst()` 内の `int()` に防御がない

```python
def get_logical_today_jst() -> str:
    hour = int(get_setting("day_start_hour") or "4")  # ← 防御なし
```

`build_system_prompt` と `_select_best_weight_records` には `try/except ValueError` が追加されたが、根元の `get_logical_today_jst()` には未適用。この関数は6箇所以上から呼ばれており、ここに防御を入れれば上位の個別防御は冗長化する（ただし defense-in-depth として残しても害はない）。

API のバリデーション（0-23 整数のみ受付）がある以上、実際にここで例外が発生する確率は極めて低い。次回フェーズで整理する際に対応すれば十分であり、Phase 15 のクロージングをブロックする問題ではない。

---

### D: デザインレビュー（再評価）

### 再評点: 9.0 / 10（前回 8.0）

| # | 判定 | 詳細 |
|---|------|------|
| D-1 | ✅ | ラベル「1日の開始時刻」は直感的で分かりやすい。バリデーションメッセージも同期済 |
| D-2 | ✅ | 冗長な `border-bottom` が除去され、`.row` クラスの CSS ルールに統一された |

説明テキストの文面も自然な日本語で、ユーザーが迷わない表現に改善されている。

---

### U: ユーザーレビュー（再評価）

### 再評点: 9.0 / 10（前回 8.5）

| # | 判定 | 詳細 |
|---|------|------|
| U-1 | ✅ | 「変更しても過去に記録済みの食事データの日付は変わりません。」が追記され、設定変更時の影響が明確になった |

---

### 既存障害テスト修正の評価

`test_phase12_step2.py` の修正は的確。`image_utils._UPLOAD_DIR` のパッチ追加は根本原因に対する正しい対処であり、`setUp`/`tearDown` の対称性も保たれている。全810テスト PASSED は品質保証として十分。

---

### 再レビュー総合所感

Phase 15 のレビュー指摘8件はすべて正確に修正された。論理日付（`get_logical_today_jst()`）の適用範囲が `_tool_record_meal`、`get_previous_weight`、`get_history`、`get_stats`、`get_frequent_meals` に拡大され、`day_start_hour` の一貫性が実用上十分なレベルに達している。

残存する C-10（`get_logical_today_jst` 内の `int()` 防御）は改善提案として記録するが、API バリデーションにより実害発生の確率は極めて低く、Phase 15 のクロージングをブロックしない。

**Phase 15 は完了と判断する。**

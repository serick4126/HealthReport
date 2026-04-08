# Phase 22 開発プラン — バグ修正2件

## 目次
| Step | ファイル | 内容 |
|------|---------|------|
| Phase22-Step1 | static/index.html | ショートカットボタンのクリックイベント重複登録を修正 |
| Phase22-Step2 | static/stats.html | ウィジェット・サマリーモーダルのドラッグをハンドル限定に変更 |

## 概要

ユーザーから2件のバグ報告：
1. チャット欄のお気に入りショートカットボタンを押すたびにテキストが重複入力される（「朝食、朝食」「朝食、朝食、朝食」と増殖）
2. 集計ページの設定モーダル（ウィジェット・サマリー両方）で、スマホでスクロールしようとするとドラッグ並び替えが発動しスクロール不能。PCのマウスドラッグも同様

---

## Step 1: ショートカットボタンのクリックイベント重複登録修正

### 対象ファイル
- `static/index.html`

### 原因分析
`loadQuickPalette()`（index.html:1248-1275）が呼ばれるたびに `chips.addEventListener('click', ...)` で**新しいリスナーを追加**している。`innerHTML` でボタンを再生成しても、イベントリスナーは `#qpChips` コンテナ要素に蓄積する（イベント委譲パターン）。

`refreshDashboard()` → `loadQuickPalette()` はログイン・チャット送信後等で複数回呼ばれるため、リスナーがN個蓄積し1タップでN回 `sendQuick()` が発火する。

### 修正内容
`chips.addEventListener('click', ...)` を `loadQuickPalette()` の外に移動し、ページ初期化時に1回だけ登録する。

**修正前（index.html:1264-1272）:**
```javascript
chips.innerHTML = html;
chips.addEventListener('click', function(e) {
  const btn = e.target.closest('.qp-chip');
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === 'weight') openWeightModal();
  else if (action === 'steps') openStepsModal();
  else if (action === 'quick') sendQuick(btn);
});
```

**修正後:**
```javascript
// loadQuickPalette() 内 — リスナー登録を削除
chips.innerHTML = html;
// addEventListener は削除

// ページ初期化部分（loadQuickPalette関数の外）に1回だけ登録
document.getElementById('qpChips').addEventListener('click', function(e) {
  const btn = e.target.closest('.qp-chip');
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === 'weight') openWeightModal();
  else if (action === 'steps') openStepsModal();
  else if (action === 'quick') sendQuick(btn);
});
```

---

## Step 2: 設定モーダルのドラッグをハンドル限定に変更

### 対象ファイル
- `static/stats.html`

### 原因分析
- **タッチ（モバイル）:** `onTouchStart` が `.widget-item` 全体に登録され、`e.preventDefault()` で即座にスクロールをブロックしている（stats.html:1329-1334, 1526-1531）
- **マウス（PC）:** `li.draggable = true` がアイテム全体に設定されており、どこをドラッグしても並び替えが発動（stats.html:1263, 1464）

### 修正内容

#### A. タッチイベント（モバイル）— 2箇所同一パターン

**ウィジェットモーダル `attachDragEvents()`（L1329-1334）:**
```javascript
// 修正前
function onTouchStart(e) {
  if (e.target.closest('.widget-toggle')) return;
  e.preventDefault();
  _touchDragState.srcEl = item;
  item.classList.add('drag-over');
}

// 修正後 — ハンドル以外はスクロール許可
function onTouchStart(e) {
  if (!e.target.closest('.widget-drag-handle')) return;
  e.preventDefault();
  _touchDragState.srcEl = item;
  item.classList.add('drag-over');
}
```

**サマリーモーダル `attachSummaryDragEvents()`（L1526-1531）:** 同一パターン適用。

#### B. HTML5 Drag and Drop（PC）

**`renderWidgetList()`（L1263）と `renderSummaryList()`（L1464）:**
```javascript
// 修正前
li.draggable = true;

// 修正後
li.draggable = false;
```

**`attachDragEvents()` と `attachSummaryDragEvents()` 内で、ハンドルの mousedown/dragend でトグル:**
```javascript
var handle = item.querySelector('.widget-drag-handle');
handle.addEventListener('mousedown', function() { item.draggable = true; });
item.addEventListener('dragend', function() {
  // 既存のdragend処理に加えて:
  item.draggable = false;
});
```

#### C. CSS変更

```css
/* 修正前 */
.widget-item { cursor: grab; ... }

/* 修正後 */
.widget-item { /* cursor: grab を削除 */ }
.widget-drag-handle { cursor: grab; }
```

---

## 検証方法

### Step 1 検証
1. アプリ起動・ログイン
2. ショートカットボタン（朝食・昼食等）を複数回タップ → テキスト入力欄に1回分だけ入力されることを確認
3. チャット送信後（`refreshDashboard` 再実行後）に再度ボタンタップ → 重複しないことを確認
4. 体重・歩数ボタンも正常動作確認

### Step 2 検証
1. 集計ページ → 表示設定モーダルを開く
2. **スマホ:** リスト部分を上下スワイプ → スクロールできることを確認
3. **スマホ:** ☰アイコンをタッチ&ドラッグ → 並び替えが動作することを確認
4. **PC:** リスト項目本体をドラッグ → 並び替えが**動かない**ことを確認
5. **PC:** ☰アイコンをクリック&ドラッグ → 並び替えが動作することを確認
6. サマリー設定モーダルでも同様に確認
7. `uv run pytest test/ -v` で既存テスト全PASSED確認

---

## 実施結果

### Step 1 実施結果
- **修正日:** 2026-04-08
- **修正ファイル:** `static/index.html`
- **修正内容:** `loadQuickPalette()` 内の `chips.addEventListener('click', ...)` を削除し、ページ初期化部分（`checkAuth()` 直前）に1回だけのイベント委譲リスナーを登録
- **テスト結果:** 989 passed, 18 warnings（全PASSED）
- **コミット:** `119d6de`

### Step 2 実施結果
- **修正日:** 2026-04-08
- **修正ファイル:** `static/stats.html`
- **修正内容:**
  - CSS: `.widget-item` から `cursor: grab` を削除、`.widget-drag-handle` に `cursor: grab` を追加
  - `renderWidgetList()` / `renderSummaryList()`: `li.draggable = true` → `false` に変更
  - `attachDragEvents()` / `attachSummaryDragEvents()`: ハンドルの `mousedown` で `draggable = true`、`dragend` で `false` に戻すトグル追加
  - タッチ `onTouchStart`: `.widget-toggle` 除外チェック → `.widget-drag-handle` 必須チェックに変更（ハンドル以外はスクロール許可）
  - 上記をウィジェット・サマリー両モーダルに適用
- **テスト結果:** 989 passed, 18 warnings（全PASSED）
- **コミット:** `a35d0ac`

---

## サマリー

Phase 22全2ステップ完了。

1. **ショートカットボタン重複入力バグ:** `loadQuickPalette()` が呼ばれるたびにイベントリスナーが蓄積していた問題を、初期化時の1回登録に修正
2. **設定モーダルのスクロール/ドラッグ競合:** タッチ・マウス両方で、ドラッグ操作を☰ハンドルアイコンからの開始に限定。リスト本体のタッチはスクロールとして動作するように変更

**レビュアーへの報告事項:**
- プラン通りの実装。変更なし
- 既存テスト989件全PASSED。新規テスト追加なし（フロントエンドのみの変更でDOM操作テストは既存テスト範囲外）

### レビュー後修正
- **修正日:** 2026-04-08
- **対応項目:** C-2a（mouseup で draggable リセット）
- **修正ファイル:** `static/stats.html`
- **修正内容:** `attachDragEvents()` / `attachSummaryDragEvents()` でハンドルの `mouseup` 時に `item.draggable = false` を追加
- **テスト結果:** 989 passed, 18 warnings（全PASSED）

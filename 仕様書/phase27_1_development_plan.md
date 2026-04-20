# Phase 27.1 開発プラン：サイドバー改善（ピン固定・Esc・bfcache対応）

作成日: 2026-04-20
担当: claude-sonnet-4-6
レビュー参照: `仕様書/phase27_review_and_recommendations.md`

---

## 目次

| Step | ファイル | 内容 |
|------|---------|------|
| Phase27.1-Step1 | database.py・main.py | `sidebar_pinned` app_settings追加 |
| Phase27.1-Step2 | common.js | 非同期DB取得・ピンボタン・localStorage廃止・Esc・pageshow対応 |
| Phase27.1-Step3 | 全5HTML | FOUCインラインスクリプト削除 |
| Phase27.1-Step4 | test/ | テスト作成・実行 |

---

## 概要

Phase 27 レビュー（C-1・C-2/U-3・U-1）の指摘を解消する。

### 変更方針まとめ

| 項目 | 決定内容 |
|------|---------|
| サイドバー状態管理 | localStorageを廃止し、DBのみに従う |
| ピン固定 | PC（>768px）のみ有効。DB `sidebar_pinned` に即時保存 |
| ピン中のハンバーガー | 非表示 |
| ピンボタン外観 | 📌アイコン（サイドバー右上）。ON時に色で区別 |
| ピン解除時のデフォルト | ページロード時は常に閉じた状態 |
| FOUC | 許容する（FOUCスクリプトを削除） |
| bfcache対応 | `pageshow` イベントで `e.persisted=true` 時にDB再取得 |
| Escキー | サイドバーが開いている時に閉じる |

---

## Step 1: `sidebar_pinned` app_settings追加

### 変更ファイル

**`database.py`** — デフォルト値を追加

```python
# get_all_settings() または DEFAULT_SETTINGS に追加
'sidebar_pinned': 'false',
```

**`main.py`** — EDITABLE_SETTINGS に追加

```python
# EDITABLE_SETTINGS リストに追加
'sidebar_pinned',
```

※ `settings.html` にはUI要素を追加しない（ピンボタンから直接保存するため）。ただし PUT /api/settings が `sidebar_pinned` キーを受け付けることを確認。

### 完了条件
- `GET /api/settings` レスポンスに `sidebar_pinned: "false"` が含まれること
- `PUT /api/settings` で `{"sidebar_pinned": "true"}` を送信して200が返ること

---

## Step 2: common.js 全面改修

### 2-1. initNav() を非同期化・DB取得に変更

```javascript
async function initNav(currentPage) {
  if (document.getElementById('sidebar')) return;

  // DB からピン状態を取得
  var pinned = false;
  var viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
  var isPC = viewportWidth > 768;
  try {
    var r = await fetch('/api/settings');
    if (r.ok) {
      var s = await r.json();
      pinned = isPC && s.sidebar_pinned === 'true';
    }
  } catch (_) {}

  // ハンバーガーボタン生成（ピン中は非表示）
  var hamburger;
  var header = document.querySelector('.header');
  if (header) {
    hamburger = document.createElement('button');
    hamburger.id = 'hamburger';
    hamburger.setAttribute('aria-label', 'メニューを開く');
    hamburger.innerHTML = '<span></span><span></span><span></span>';
    hamburger.addEventListener('click', function() {
      if (document.body.classList.contains('sidebar-open')) {
        closeSidebar();
      } else {
        openSidebar();
      }
    });
    if (pinned) hamburger.style.display = 'none';
    header.insertBefore(hamburger, header.firstChild);
  }

  // ピン状態に応じてbody.sidebar-openを設定
  if (pinned) {
    document.body.classList.add('sidebar-open');
  } else {
    document.body.classList.remove('sidebar-open');
  }

  // サイドバー生成
  var sidebar = document.createElement('div');
  sidebar.id = 'sidebar';

  var appTitle = document.createElement('div');
  appTitle.className = 'sidebar-app-title';

  var titleText = document.createElement('span');
  titleText.textContent = '🌿 HealthReport';
  appTitle.appendChild(titleText);

  // ピンボタン（PCのみ）
  if (isPC) {
    var pinBtn = document.createElement('button');
    pinBtn.id = 'sidebar-pin-btn';
    pinBtn.className = 'sidebar-pin-btn' + (pinned ? ' pinned' : '');
    pinBtn.setAttribute('aria-label', pinned ? 'サイドバーの固定を解除' : 'サイドバーを固定');
    pinBtn.textContent = '📌';
    pinBtn.addEventListener('click', togglePin);
    appTitle.appendChild(pinBtn);
  }

  sidebar.appendChild(appTitle);

  // ナビ項目
  var nav = document.createElement('nav');
  nav.className = 'sidebar-nav';
  NAV_ITEMS.forEach(function(item) {
    var el;
    if (item.id === currentPage) {
      el = document.createElement('span');
      el.className = 'sidebar-nav-item current';
    } else {
      el = document.createElement('a');
      el.className = 'sidebar-nav-item';
      el.href = item.href;
    }
    el.innerHTML = item.icon + ' <span class="sidebar-nav-label">' + escHtml(item.label) + '</span>';
    nav.appendChild(el);
  });
  sidebar.appendChild(nav);

  // フッターボタン
  var footer = document.createElement('div');
  footer.className = 'sidebar-footer';
  var btnReset = document.createElement('button');
  btnReset.className = 'sidebar-footer-btn';
  btnReset.innerHTML = '🔄 <span>会話リセット</span>';
  btnReset.addEventListener('click', clearHistory);
  var btnLogout = document.createElement('button');
  btnLogout.className = 'sidebar-footer-btn';
  btnLogout.innerHTML = '🚪 <span>ログアウト</span>';
  btnLogout.addEventListener('click', doLogout);
  footer.appendChild(btnReset);
  footer.appendChild(btnLogout);
  sidebar.appendChild(footer);

  // オーバーレイ
  var overlay = document.createElement('div');
  overlay.id = 'sidebar-overlay';
  overlay.addEventListener('click', closeSidebar);

  // DOM注入
  document.body.insertBefore(overlay, document.body.firstChild);
  document.body.insertBefore(sidebar, document.body.firstChild);

  // aria-expanded 初期設定
  if (hamburger) {
    hamburger.setAttribute('aria-expanded', pinned ? 'true' : 'false');
  }
}
```

### 2-2. openSidebar() / closeSidebar() に aria-label 更新を追加

```javascript
function openSidebar() {
  document.body.classList.add('sidebar-open');
  var h = document.getElementById('hamburger');
  if (h) {
    h.setAttribute('aria-expanded', 'true');
    h.setAttribute('aria-label', 'メニューを閉じる');
  }
}

function closeSidebar() {
  document.body.classList.remove('sidebar-open');
  var h = document.getElementById('hamburger');
  if (h) {
    h.setAttribute('aria-expanded', 'false');
    h.setAttribute('aria-label', 'メニューを開く');
  }
}
```

### 2-3. togglePin() 関数追加

```javascript
async function togglePin() {
  var pinBtn = document.getElementById('sidebar-pin-btn');
  if (!pinBtn) return;
  var nowPinned = pinBtn.classList.contains('pinned');
  var next = !nowPinned;

  pinBtn.classList.toggle('pinned', next);
  pinBtn.setAttribute('aria-label', next ? 'サイドバーの固定を解除' : 'サイドバーを固定');

  var hamburger = document.getElementById('hamburger');
  if (hamburger) hamburger.style.display = next ? 'none' : '';

  try {
    await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sidebar_pinned: next ? 'true' : 'false' })
    });
  } catch (_) {}
}
```

### 2-4. Esc キーリスナー追加（initNav() 末尾）

```javascript
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
    var pinBtn = document.getElementById('sidebar-pin-btn');
    var isPinned = pinBtn && pinBtn.classList.contains('pinned');
    if (!isPinned) {
      closeSidebar();
      var h = document.getElementById('hamburger');
      if (h) h.focus();
    }
  }
});
```

### 2-5. pageshow イベントで bfcache 対応（initNav() 末尾）

```javascript
window.addEventListener('pageshow', function(e) {
  if (!e.persisted) return;
  var viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
  var isPC = viewportWidth > 768;
  fetch('/api/settings').then(function(r) {
    if (!r.ok) return;
    return r.json();
  }).then(function(s) {
    if (!s) return;
    var pinned = isPC && s.sidebar_pinned === 'true';
    if (pinned) {
      document.body.classList.add('sidebar-open');
      var h = document.getElementById('hamburger');
      if (h) h.style.display = 'none';
    } else {
      document.body.classList.remove('sidebar-open');
      var h2 = document.getElementById('hamburger');
      if (h2) h2.style.display = '';
    }
    var pinBtn = document.getElementById('sidebar-pin-btn');
    if (pinBtn) {
      pinBtn.classList.toggle('pinned', pinned);
      pinBtn.setAttribute('aria-label', pinned ? 'サイドバーの固定を解除' : 'サイドバーを固定');
    }
  }).catch(function() {});
});
```

### 2-6. localStorage関連コードをすべて削除

- `openSidebar()` / `closeSidebar()` から `localStorage.setItem(...)` 行を削除
- `initNav()` 内の `localStorage.getItem('healthreport_sidebar')` ロジックを削除

---

## Step 3: 全5HTMLのFOUCスクリプト削除

以下の5ファイルから `</head>` 直前のFOUCインラインスクリプトブロックを削除する：

```
static/index.html
static/history.html
static/stats.html
static/report.html
static/settings.html
```

削除対象ブロック（各ファイルで同一または類似）：

```html
<script>
  (function(){
    try { var p=localStorage.getItem('healthreport_sidebar');
      if(window.innerWidth>768 && p!=='closed') document.documentElement.classList.add('sidebar-open');
    } catch(_){}
  })();
</script>
```

---

## Step 4: テスト作成・実行

### テストファイル: `test/test_phase27_1.py`

- `sidebar_pinned` のDB保存・取得テスト（tempfile DB使用）
- `PUT /api/settings` で `sidebar_pinned` を更新できることを確認
- `GET /api/settings` に `sidebar_pinned` が含まれることを確認
- デフォルト値が `"false"` であることを確認

---

## CSSへの追加（common.cssまたはインライン）

```css
/* ピンボタン */
.sidebar-app-title {
  display: flex;
  align-items: center;
}

.sidebar-pin-btn {
  margin-left: auto;
  background: none;
  border: none;
  font-size: 16px;
  cursor: pointer;
  opacity: 0.4;
  padding: 4px 6px;
  border-radius: 4px;
  transition: opacity 0.2s;
}

.sidebar-pin-btn:hover {
  opacity: 0.8;
}

.sidebar-pin-btn.pinned {
  opacity: 1;
  background: var(--accent-light, rgba(0,128,0,0.15));
}
```

---

## 実施結果

### Phase27.1-Step1（2026-04-21）
- 修正ファイル: `database.py`（sidebar_pinnedデフォルト値追加）、`main.py`（EDITABLE_SETTINGS・plain_keys追加）
- テスト: コードレビューで完了条件確認（GET/PUT動作を静的確認）
- レビュー結果: ✅ Spec compliant / ✅ Code quality approved

### Phase27.1-Step2（2026-04-21）
- 修正ファイル: `static/common.js`（非同期化・ピンボタン・localStorage廃止・Esc・pageshow・関数分割）、`static/common.css`（ピンボタンCSS追加・text-overflow修正）
- テスト: Step4で実施予定
- レビュー結果: ✅ Spec compliant / ✅ Code quality approved（C-2ロールバック追加・I-1関数分割・I-2CSS修正・I-3aria修正適用済み）

---

## サマリー（全Step完了後に追記）

<!-- 完了後に追記 -->

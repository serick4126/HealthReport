/* HealthReport 共通JavaScript */

// ── ナビバー生成 ──────────────────────────────────────────────────────────────
const NAV_ITEMS = [
  { id: 'chat',     icon: '💬', label: 'チャット', href: '/'         },
  { id: 'history',  icon: '📋', label: '履歴',     href: '/history'  },
  { id: 'stats',    icon: '📊', label: '集計',     href: '/stats'    },
  { id: 'report',   icon: '📄', label: 'レポート', href: '/report'   },
  { id: 'settings', icon: '⚙️', label: '設定',     href: '/settings' },
];

// ハンバーガーボタンを生成してheaderに挿入し、buttonを返す
function _buildHamburger(pinned) {
  var header = document.querySelector('.header');
  if (!header) return null;
  var hamburger = document.createElement('button');
  hamburger.id = 'hamburger';
  hamburger.setAttribute('aria-label', 'メニューを開く');
  hamburger.setAttribute('aria-expanded', pinned ? 'true' : 'false');
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
  return hamburger;
}

// サイドバーDOM全体を生成してbodyに挿入
function _buildSidebar(currentPage, pinned, isPC) {
  var sidebar = document.createElement('div');
  sidebar.id = 'sidebar';

  var appTitle = document.createElement('div');
  appTitle.className = 'sidebar-app-title';
  var titleText = document.createElement('span');
  titleText.textContent = '🌿 HealthReport';
  appTitle.appendChild(titleText);

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

  var overlay = document.createElement('div');
  overlay.id = 'sidebar-overlay';
  overlay.addEventListener('click', closeSidebar);

  document.body.insertBefore(overlay, document.body.firstChild);
  document.body.insertBefore(sidebar, document.body.firstChild);
}

// Escキー・pageshoイベントリスナー登録
function _registerNavListeners() {
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
      var pb = document.getElementById('sidebar-pin-btn');
      var isPinned = pb && pb.classList.contains('pinned');
      if (!isPinned) {
        closeSidebar();
        var h = document.getElementById('hamburger');
        if (h) h.focus();
      }
    }
  });

  // pageshow bfcache 対応
  window.addEventListener('pageshow', function(e) {
    if (!e.persisted) return;
    var vw = window.innerWidth || document.documentElement.clientWidth || 0;
    var pc = vw > 768;
    fetch('/api/settings').then(function(r) {
      if (!r.ok) return;
      return r.json();
    }).then(function(s) {
      if (!s) return;
      var p = pc && s.sidebar_pinned === 'true';
      if (p) {
        document.body.classList.add('sidebar-open');
        var h = document.getElementById('hamburger');
        if (h) h.style.display = 'none';
      } else {
        document.body.classList.remove('sidebar-open');
        var h2 = document.getElementById('hamburger');
        if (h2) {
          h2.style.display = '';
          h2.setAttribute('aria-label', 'メニューを開く');
          h2.setAttribute('aria-expanded', 'false');
        }
      }
      var pb = document.getElementById('sidebar-pin-btn');
      if (pb) {
        pb.classList.toggle('pinned', p);
        pb.setAttribute('aria-label', p ? 'サイドバーの固定を解除' : 'サイドバーを固定');
      }
    }).catch(function() {});
  });
}

async function initNav(currentPage) {
  if (document.getElementById('sidebar')) return;

  // DBからピン状態を取得
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

  // ピン状態に応じてbody.sidebar-openを設定
  if (pinned) {
    document.body.classList.add('sidebar-open');
  } else {
    document.body.classList.remove('sidebar-open');
  }

  _buildHamburger(pinned);
  _buildSidebar(currentPage, pinned, isPC);
  _registerNavListeners();
}

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

async function togglePin() {
  var pinBtn = document.getElementById('sidebar-pin-btn');
  if (!pinBtn) return;
  var nowPinned = pinBtn.classList.contains('pinned');
  var next = !nowPinned;

  // 楽観的UI更新
  pinBtn.classList.toggle('pinned', next);
  pinBtn.setAttribute('aria-label', next ? 'サイドバーの固定を解除' : 'サイドバーを固定');
  var hamburger = document.getElementById('hamburger');
  if (hamburger) hamburger.style.display = next ? 'none' : '';

  try {
    var res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sidebar_pinned: next ? 'true' : 'false' })
    });
    if (!res.ok) throw new Error('save failed');
  } catch (_) {
    // ロールバック
    pinBtn.classList.toggle('pinned', nowPinned);
    pinBtn.setAttribute('aria-label', nowPinned ? 'サイドバーの固定を解除' : 'サイドバーを固定');
    if (hamburger) hamburger.style.display = nowPinned ? 'none' : '';
  }
}

// ── 共通：会話リセット・ログアウト（index.html でオーバーライド）─────────────
async function clearHistory() {
  if (!confirm('会話履歴をリセットしますか？（記録済みのデータは消えません）')) return;
  await fetch('/api/chat/history', { method: 'DELETE' });
  location.href = '/';
}

async function doLogout() {
  await fetch('/api/logout', { method: 'POST' });
  location.href = '/';
}

// ── HTML エスケープ ───────────────────────────────────────────────────────────
function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── テーマ初期化 ──────────────────────────────────────────────────────────────
async function initTheme() {
  // まずlocalStorageから即座に適用（FOUC防止）
  try {
    var cached = localStorage.getItem('healthreport_theme');
    if (cached) {
      document.documentElement.dataset.theme = cached;
    }
  } catch (_) {
    // localStorage無効時（プライベートブラウジング等）はスキップ
  }
  try {
    var r = await fetch('/api/settings');
    if (!r.ok) return;
    var s = await r.json();
    var theme = s.theme || 'auto';
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem('healthreport_theme', theme);
    } catch (_) {
      // localStorage無効時はスキップ
    }
  } catch (e) { console.error('テーマ取得エラー:', e); }
}

// ── 認証チェック（未認証なら / へリダイレクト）──────────────────────────────
async function checkAuthOrRedirect() {
  try {
    var r = await fetch('/api/me');
    if (!r.ok) location.href = '/';
  } catch (_) {
    location.href = '/';
  }
}

// ── 共通 fetch エラーハンドラー ───────────────────────────────────────────────
/**
 * fetch 失敗時の共通処理。
 * @param {Element|null} container - エラーメッセージを表示する要素
 * @param {Error|Response|unknown} e  - catch したエラーまたは Response オブジェクト
 */
function handleFetchError(container, e) {
  console.error('fetchエラー:', e);

  // Response オブジェクト（!res.ok → throw res パターン）または Error オブジェクトを受け取る
  var status = (e instanceof Response) ? e.status : 0;
  var isNetworkError = e instanceof TypeError;

  if (status === 401) {
    location.href = '/';
    return;
  }

  var msg;
  if (isNetworkError) {
    msg = 'ネットワークエラーが発生しました。接続を確認してください。';
  } else if (status >= 500) {
    msg = 'サーバーエラーが発生しました。しばらく待ってからお試しください。';
  } else {
    msg = 'エラーが発生しました。再度お試しください。';
  }

  if (container) {
    container.innerHTML = '<p class="error-msg">' + escHtml(msg) + '</p>';
  }
}

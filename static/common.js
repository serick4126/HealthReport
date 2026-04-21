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
function _buildHamburger() {
  var header = document.querySelector('.header');
  if (!header) return null;
  var hamburger = document.createElement('button');
  hamburger.id = 'hamburger';
  hamburger.setAttribute('aria-label', 'メニューを開く');
  hamburger.setAttribute('aria-expanded', 'false');
  hamburger.innerHTML = '<span></span><span></span><span></span>';
  hamburger.addEventListener('click', function() {
    if (document.body.classList.contains('sidebar-open')) {
      closeSidebar();
    } else {
      openSidebar();
    }
  });
  header.insertBefore(hamburger, header.firstChild);
  return hamburger;
}

// サイドバーDOM全体を生成してbodyに挿入
function _buildSidebar(currentPage) {
  var sidebar = document.createElement('div');
  sidebar.id = 'sidebar';

  var appTitle = document.createElement('div');
  appTitle.className = 'sidebar-app-title';
  var titleText = document.createElement('span');
  titleText.textContent = '🌿 HealthReport';
  appTitle.appendChild(titleText);
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

// Escキー・pageshowイベントリスナー登録
function _registerNavListeners() {
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
      closeSidebar();
      var h = document.getElementById('hamburger');
      if (h) h.focus();
    }
  });

  // pageshow bfcache 対応（モバイルでbfcacheから戻った場合にサイドバーが開いたままになるのを防ぐ。PC時はsidebar-openを設定しないためno-op）
  window.addEventListener('pageshow', function(e) {
    if (!e.persisted) return;
    document.body.classList.remove('sidebar-open');
  });
}

async function initNav(currentPage) {
  if (document.getElementById('sidebar')) return;

  var viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
  var isPC = viewportWidth >= 768; // リサイズ非対応（リロードで反映）

  if (isPC) {
    setTimeout(function() { document.body.classList.add('nav-ready'); }, 1000);
  }

  if (!isPC) {
    _buildHamburger();
  }
  _buildSidebar(currentPage);
  _registerNavListeners();
  if (isPC) {
    document.body.classList.add('nav-ready');
  }
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

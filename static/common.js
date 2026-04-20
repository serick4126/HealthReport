/* HealthReport 共通JavaScript */

// ── ナビバー生成 ──────────────────────────────────────────────────────────────
const NAV_ITEMS = [
  { id: 'chat',     icon: '💬', label: 'チャット', href: '/'         },
  { id: 'history',  icon: '📋', label: '履歴',     href: '/history'  },
  { id: 'stats',    icon: '📊', label: '集計',     href: '/stats'    },
  { id: 'report',   icon: '📄', label: 'レポート', href: '/report'   },
  { id: 'settings', icon: '⚙️', label: '設定',     href: '/settings' },
];

function initNav(currentPage) {
  if (document.getElementById('sidebar')) return;
  // ハンバーガーボタンをヘッダー先頭に挿入
  var header = document.querySelector('.header');
  if (header) {
    var hamburger = document.createElement('button');
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
    header.insertBefore(hamburger, header.firstChild);
  }

  // サイドバー生成
  var sidebar = document.createElement('div');
  sidebar.id = 'sidebar';

  var appTitle = document.createElement('div');
  appTitle.className = 'sidebar-app-title';
  appTitle.textContent = '🌿 HealthReport';
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

  // オーバーレイ生成
  var overlay = document.createElement('div');
  overlay.id = 'sidebar-overlay';
  overlay.addEventListener('click', closeSidebar);

  // DOMへ注入（sidebarが最前面）
  document.body.insertBefore(overlay, document.body.firstChild);
  document.body.insertBefore(sidebar, document.body.firstChild);
}

function openSidebar() {
  document.body.classList.add('sidebar-open');
  var h = document.getElementById('hamburger');
  if (h) h.setAttribute('aria-expanded', 'true');
  try { localStorage.setItem('healthreport_sidebar', 'open'); } catch(_) {}
}

function closeSidebar() {
  document.body.classList.remove('sidebar-open');
  var h = document.getElementById('hamburger');
  if (h) h.setAttribute('aria-expanded', 'false');
  try { localStorage.setItem('healthreport_sidebar', 'closed'); } catch(_) {}
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

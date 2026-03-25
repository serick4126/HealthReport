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
  var el = document.getElementById('headerNav');
  if (!el) return;
  var items = NAV_ITEMS.map(function(item) {
    if (item.id === currentPage) {
      return '<span class="nav-btn current" title="' + item.label + '">' + item.icon + '<span class="nl"> ' + item.label + '</span></span>';
    }
    return '<a href="' + item.href + '" class="nav-btn" title="' + item.label + '">' + item.icon + '<span class="nl"> ' + item.label + '</span></a>';
  });
  items.push('<button class="nav-btn nav-reset" title="会話履歴をリセット" onclick="clearHistory()">🔄</button>');
  items.push('<button class="nav-btn" onclick="doLogout()" title="ログアウト">🚪<span class="nl"> ログアウト</span></button>');
  el.innerHTML = items.join('');
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
  // バックグラウンドで最新を取得して更新
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
  } catch (_) {}
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

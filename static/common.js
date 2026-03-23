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
  items.push('<button class="nav-btn" title="会話履歴をリセット" onclick="clearHistory()">🔄</button>');
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

// ── 認証チェック（未認証なら / へリダイレクト）──────────────────────────────
async function checkAuthOrRedirect() {
  try {
    var r = await fetch('/api/me');
    if (!r.ok) location.href = '/';
  } catch (_) {
    location.href = '/';
  }
}

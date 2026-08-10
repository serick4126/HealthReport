"""MCP エンドポイントの認証ラッパと lifespan 管理。

- 秘密URL方式: `/mcp/{token}`。トークン不一致・未設定は 404（401ではない — 設計仕様書 §4.1）
- レート制限: プロセス全体で 60 req/分。超過時 429 + 日本語の理由をボディに含める（U-2）
- 設定ハッシュ監視: instructions 動的部の元となる設定値から SHA-256 を計算し、
  変化時は MCPServer と ASGI アプリを再構築して差し替える（最大60秒に1回チェック、P-3）
- 内側アプリ（`streamable_http_app()` が返す Starlette アプリ）は `app.mount()` では
  lifespan が起動しないため、`main.py` の lifespan から `mcp_lifespan()` で開始する（P-1）
"""
import asyncio
import hashlib
import hmac
import inspect
import logging
import time
from collections import deque
from contextlib import asynccontextmanager, AsyncExitStack

from starlette.responses import JSONResponse, PlainTextResponse

import database
import mcp_server

logger = logging.getLogger(__name__)

RATE_LIMIT_MAX = 60  # 認証済み: 1分あたりの最大リクエスト数
RATE_LIMIT_UNAUTH_MAX = 300  # トークン照合前: 過剰なDBアクセスとログ肥大を抑えるための上限
# （43文字の token_urlsafe に総当たりは成立しないため、総当たり対策が目的ではない）
RATE_LIMIT_WINDOW_SEC = 60.0
RATE_LIMIT_MESSAGE = (
    "リクエストが多すぎます。1分間に60回までです。少し待ってから再試行してください。"
)
REBUILD_CHECK_INTERVAL_SEC = 60.0  # 設定ハッシュの再チェック間隔（秒）


def _hit_rate_limit(timestamps: deque, now: float, limit: int) -> bool:
    """タイムスタンプを記録し、窓内の件数が limit を超えたら True を返す。

    超過時は自分自身のタイムスタンプを取り消す。取り消さないと、拒否された
    リクエストが窓を埋め続け、送信が止まるまで永久に回復しない（C-3）。
    """
    timestamps.append(now)
    while timestamps and now - timestamps[0] > RATE_LIMIT_WINDOW_SEC:
        timestamps.popleft()
    if len(timestamps) > limit:
        timestamps.pop()
        return True
    return False

# 設定ハッシュの対象キー（mcp_server の動的部定義から導出。両者が乖離しない一元化）
_CONFIG_HASH_KEYS = list(mcp_server.DYNAMIC_INSTRUCTION_KEYS)

_mcp_app = None  # 内側 Starlette アプリ（lifespan 中に構築）
_settings_hash: str | None = None  # 最後に検知した設定ハッシュ
_last_hash_check_at: float | None = None  # 最後にハッシュを確認した時刻（time.monotonic 基準）
_rebuild_lock = asyncio.Lock()  # 再構築中の並行リクエストを直列化


def _compute_settings_hash() -> str:
    """instructions 動的部の元となる設定値の連結文字列から SHA-256 を計算する。"""
    parts = [database.get_setting(key) or "" for key in _CONFIG_HASH_KEYS]
    return hashlib.sha256("\u0000".join(parts).encode("utf-8")).hexdigest()


def _build_inner_app():
    """MCPServer と ASGI アプリを構築する（instructions は毎回 DB から生成）。"""
    server = mcp_server.build_mcp_server(instructions=mcp_server.build_instructions())
    return mcp_server.build_mcp_asgi(server)


class _AppHost:
    """内側アプリ1つの lifespan を専有タスクで保持する。

    MCP SDK の streamable_http_manager.run() は内部で anyio.create_task_group() を
    使うため、(a) enter したタスクと同じタスクでしか exit できず、(b) 同一タスク内で
    入れ子でない2つの cancel scope を LIFO 以外の順で閉じられない（C-1・実測）。
    「1アプリ = 1タスクが生涯を所有する」形にすることで、両制約を構造的に満たす。
    """

    def __init__(self, app):
        self.app = app
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task = None
        self._error = None

    async def start(self):
        self._task = asyncio.create_task(self._run())
        await self._ready.wait()
        if self._error is not None:
            raise self._error

    async def _run(self):
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(self.app.router.lifespan_context(self.app))
                self._ready.set()
                await self._stop.wait()
        except Exception as exc:
            self._error = exc
            logger.error("MCP内側アプリの lifespan が異常終了しました", exc_info=True)
        finally:
            self._ready.set()  # 起動失敗時も待機を解除する（start() が永久に待たないように）

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            await self._task


_host: "_AppHost | None" = None


async def _rebuild(new_hash: str) -> None:
    """新アプリを別タスクで起動してから差し替え、旧アプリを旧タスク自身に閉じさせる。

    順序は「新start → 原子swap → 旧stop」。この間ずっと有効なアプリが存在するため、
    並行リクエストが閉じたアプリへ委譲されることがない（P-1）。
    """
    global _host, _mcp_app, _settings_hash
    new_host = _AppHost(_build_inner_app())
    await new_host.start()  # 新: 自分のタスクで enter
    old_host = _host
    _host, _mcp_app, _settings_hash = new_host, new_host.app, new_hash  # 原子swap
    if old_host is not None:
        await old_host.stop()  # 旧: 旧タスク自身が exit
    logger.info("MCP server rebuilt (settings changed)")


async def _maybe_rebuild():
    """最大60秒に1回、設定ハッシュを確認し変化時に再構築する。"""
    global _last_hash_check_at
    if _host is None:
        logger.debug("MCP app host is not running; skip rebuild")  # P-17
        return
    now = time.monotonic()
    if _last_hash_check_at is not None and now - _last_hash_check_at < REBUILD_CHECK_INTERVAL_SEC:
        return
    _last_hash_check_at = now
    new_hash = _compute_settings_hash()
    if new_hash == _settings_hash:
        return
    async with _rebuild_lock:
        if new_hash == _settings_hash:  # ロック待ちの間に他リクエストが再構築済み
            return
        try:
            await _rebuild(new_hash)
        except Exception:
            # P-18: 再構築に失敗しても旧アプリで処理を継続する。
            # _settings_hash は更新しないため、次のチェック機会に再試行される。
            logger.error("MCPサーバーの再構築に失敗しました", exc_info=True)


async def _inner_app_provider():
    await _maybe_rebuild()
    if _mcp_app is None:
        raise RuntimeError("MCP inner app is not initialized")
    return _mcp_app


async def _send_404(scope, receive, send) -> None:
    await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)


class TokenGuardASGI:
    """秘密URL方式のトークン検証ラッパ。検証成功時のみ内側アプリへ委譲する。"""

    def __init__(self, inner_app_provider):
        self._inner_app_provider = inner_app_provider
        # レート制限キューはインスタンス属性（本番は単一インスタンス = プロセス全体60req/分）
        # C-3: 未認証枠（照合前）と認証枠（照合後）を分離する
        self._unauth_timestamps = deque()
        self._auth_timestamps = deque()

    async def _send_429(self, scope, receive, send) -> None:
        await JSONResponse({"error": RATE_LIMIT_MESSAGE}, status_code=429)(
            scope, receive, send
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await _send_404(scope, receive, send)
            return

        # ── 未認証枠（トークン照合の前）。匿名の第三者によるフラッドで
        #    正当な接続を閉塞できないよう、過剰なリクエストのみ抑止する ──
        now = time.monotonic()
        if _hit_rate_limit(self._unauth_timestamps, now, RATE_LIMIT_UNAUTH_MAX):
            logger.warning("MCP unauth rate limit exceeded")
            await self._send_429(scope, receive, send)
            return

        stored = database.get_setting("mcp_api_key") or ""
        if not stored:
            await _send_404(scope, receive, send)
            return

        # ── P-8: Starlette の Mount は path からプレフィックスを剥がさない ──
        # scope["path"] は "/mcp/{token}" のまま残り、root_path に "/mcp" が入る。
        # ここでトークンを取り出し、root_path / path を書き換えて内側アプリへ渡す。
        root = scope.get("root_path", "")
        path = scope["path"]
        rel = path[len(root):] if root and path.startswith(root) else path
        token = rel.lstrip("/").split("/", 1)[0]

        if not hmac.compare_digest(token, stored):
            # トークン全体はログに出さない（長さのみ記録）
            logger.warning("MCP token mismatch (len=%d)", len(token))
            await _send_404(scope, receive, send)
            return

        # ── 認証枠（トークン照合成功後）。C-3: 誤トークンが認証枠を消費しない ──
        if _hit_rate_limit(self._auth_timestamps, now, RATE_LIMIT_MAX):
            logger.warning("MCP rate limit exceeded (%d req/min)", len(self._auth_timestamps))
            await self._send_429(scope, receive, send)
            return

        new_root = root + "/" + token
        scope = dict(scope)
        scope["root_path"] = new_root
        scope["path"] = new_root + "/"  # 末尾スラッシュ必須（無いと 307 になる — P-8）
        scope["raw_path"] = scope["path"].encode()

        inner = self._inner_app_provider()
        if inspect.isawaitable(inner):
            inner = await inner
        await inner(scope, receive, send)


@asynccontextmanager
async def mcp_lifespan():
    """内側 MCP アプリのホストタスクを起動し、終了時に停止する。"""
    global _host, _mcp_app, _settings_hash, _last_hash_check_at
    _host = _AppHost(_build_inner_app())
    await _host.start()
    _mcp_app = _host.app
    _settings_hash = _compute_settings_hash()
    _last_hash_check_at = time.monotonic()
    try:
        yield
    finally:
        # 再構築で _host が差し替わっている可能性があるため、現在の _host を停止する。
        # 旧ホストは再構築時に stop 済みのため二重停止は起きない。
        # テストで TestClient を入れ子にした場合、内側の finally が _host を None に
        # 戻した後に外側の finally が実行されるため、None ガードで安全にする。
        if _host is not None:
            await _host.stop()
        _host = None
        _mcp_app = None
        _settings_hash = None
        _last_hash_check_at = None


def get_asgi_app():
    """app.mount("/mcp", ...) に渡す ASGI アプリを返す。"""
    return TokenGuardASGI(_inner_app_provider)

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

RATE_LIMIT_MAX = 60  # 1分あたりの最大リクエスト数（プロセス全体）
RATE_LIMIT_WINDOW_SEC = 60.0
RATE_LIMIT_MESSAGE = (
    "リクエストが多すぎます。1分間に60回までです。少し待ってから再試行してください。"
)
REBUILD_CHECK_INTERVAL_SEC = 60.0  # 設定ハッシュの再チェック間隔（秒）

# 設定ハッシュの対象キー（instructions 動的部の元となる設定値）
_CONFIG_HASH_KEYS = [
    "user_name",
    "user_height_cm",
    "user_gender",
    "user_birthdate",
    "daily_calorie_goal",
    "daily_steps_goal",
    "day_start_hour",
    "user_notes",
]

_mcp_app = None  # 内側 Starlette アプリ（lifespan 中に構築）
_mcp_stack = None  # 内側アプリの lifespan を保持する AsyncExitStack
_settings_hash: str | None = None  # 最後に検知した設定ハッシュ
_last_hash_check_at: float | None = None  # 最後にハッシュを確認した時刻（time.monotonic 基準）
_rebuild_lock = asyncio.Lock()  # 再構築中の並行リクエストを直列化


def _compute_settings_hash() -> str:
    """instructions 動的部の元となる設定値の連結文字列から SHA-256 を計算する。"""
    parts = [database.get_setting(key) or "" for key in _CONFIG_HASH_KEYS]
    return hashlib.sha256("\u0000".join(parts).encode("utf-8")).hexdigest()


async def _rebuild_server(new_hash: str):
    """新しい MCPServer と ASGI アプリを構築し、lifespan を差し替える（P-1/P-3）。

    順序は「新 enter → 原子swap → 旧 close」。旧 close を先にすると、swap までの
    窓で並行リクエストが閉じた旧アプリへ委譲され MCP 内部で 500 になる。
    enter 失敗時も旧アプリが生きており、`_mcp_app` は常に有効な旧/新のどちらかになる。
    """
    global _mcp_app, _mcp_stack, _settings_hash
    server = mcp_server.build_mcp_server(instructions=mcp_server.build_instructions())
    new_app = mcp_server.build_mcp_asgi(server)
    old_stack = _mcp_stack
    new_stack = AsyncExitStack()
    await new_stack.enter_async_context(  # 新 lifespan を先に有効化（P-1）
        new_app.router.lifespan_context(new_app)
    )
    _mcp_stack, _mcp_app, _settings_hash = new_stack, new_app, new_hash  # 原子差し替え
    if old_stack is not None:
        await old_stack.aclose()  # 旧 lifespan を最後に閉鎖（P-1）
    logger.info("MCP server rebuilt (settings changed)")


async def _maybe_rebuild():
    """最大60秒に1回、設定ハッシュを確認し変化時に MCPServer/ASGI を再構築する（P-3）。"""
    global _last_hash_check_at
    now = time.monotonic()
    if _last_hash_check_at is not None and now - _last_hash_check_at < REBUILD_CHECK_INTERVAL_SEC:
        return
    _last_hash_check_at = now
    new_hash = _compute_settings_hash()
    if new_hash == _settings_hash:
        return
    async with _rebuild_lock:
        # ロック待ちの間に別リクエストが再構築済みの可能性があるため再確認する
        if new_hash != _settings_hash:
            await _rebuild_server(new_hash)


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
        self._rate_timestamps = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await _send_404(scope, receive, send)
            return

        # ── レート制限（トークン照合の前） ──
        now = time.monotonic()
        self._rate_timestamps.append(now)
        while self._rate_timestamps and now - self._rate_timestamps[0] > RATE_LIMIT_WINDOW_SEC:
            self._rate_timestamps.popleft()  # 60秒より古いタイムスタンプを破棄
        if len(self._rate_timestamps) > RATE_LIMIT_MAX:
            logger.warning("MCP rate limit exceeded (%d req/min)", len(self._rate_timestamps))
            await JSONResponse({"error": RATE_LIMIT_MESSAGE}, status_code=429)(
                scope, receive, send
            )
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
    """内側 MCP アプリを構築し、その lifespan（セッションマネージャ）を開始する。"""
    global _mcp_app, _mcp_stack, _settings_hash, _last_hash_check_at
    server = mcp_server.build_mcp_server(instructions=mcp_server.build_instructions())
    inner = mcp_server.build_mcp_asgi(server)
    _mcp_app = inner
    stack = AsyncExitStack()
    _mcp_stack = stack
    try:
        await stack.enter_async_context(inner.router.lifespan_context(inner))
        # 起動時点の設定を基準ハッシュにし、最初のリクエストで無駄な再構築をしない
        _settings_hash = _compute_settings_hash()
        _last_hash_check_at = time.monotonic()
        yield
    finally:
        await stack.aclose()
        _mcp_stack = None
        _mcp_app = None
        _settings_hash = None
        _last_hash_check_at = None


def get_asgi_app():
    """app.mount("/mcp", ...) に渡す ASGI アプリを返す。"""
    return TokenGuardASGI(_inner_app_provider)

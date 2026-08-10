"""MCP エンドポイントの認証ラッパと lifespan 管理。

- 秘密URL方式: `/mcp/{token}`。トークン不一致・未設定は 404（401ではない — 設計仕様書 §4.1）
- 内側アプリ（`streamable_http_app()` が返す Starlette アプリ）は `app.mount()` では
  lifespan が起動しないため、`main.py` の lifespan から `mcp_lifespan()` で開始する（P-1）
"""
import hmac
import logging
from contextlib import asynccontextmanager, AsyncExitStack

from starlette.responses import PlainTextResponse

import database
import mcp_server

logger = logging.getLogger(__name__)

_mcp_app = None  # 内側 Starlette アプリ（lifespan 中に構築）
_mcp_stack = None  # 内側アプリの lifespan を保持する AsyncExitStack


def _inner_app_provider():
    if _mcp_app is None:
        raise RuntimeError("MCP inner app is not initialized")
    return _mcp_app


async def _send_404(scope, receive, send) -> None:
    await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)


class TokenGuardASGI:
    """秘密URL方式のトークン検証ラッパ。検証成功時のみ内側アプリへ委譲する。"""

    def __init__(self, inner_app_provider):
        self._inner_app_provider = inner_app_provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await _send_404(scope, receive, send)
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
        await self._inner_app_provider()(scope, receive, send)


@asynccontextmanager
async def mcp_lifespan():
    """内側 MCP アプリを構築し、その lifespan（セッションマネージャ）を開始する。"""
    global _mcp_app, _mcp_stack
    server = mcp_server.build_mcp_server()
    inner = mcp_server.build_mcp_asgi(server)
    _mcp_app = inner
    stack = AsyncExitStack()
    _mcp_stack = stack
    try:
        await stack.enter_async_context(inner.router.lifespan_context(inner))
        yield
    finally:
        await stack.aclose()
        _mcp_stack = None
        _mcp_app = None


def get_asgi_app():
    """app.mount("/mcp", ...) に渡す ASGI アプリを返す。"""
    return TokenGuardASGI(_inner_app_provider)

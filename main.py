import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

load_dotenv()

import claude_client
import database

# ── セッション管理（シングルユーザー・インメモリ） ────────────────────────────

sessions: set[str] = set()

# 会話履歴（シングルユーザー・インメモリ）
conversation_history: list[dict] = []

STATIC_DIR = Path(__file__).parent / "static"


# ── アプリ初期化 ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(lifespan=lifespan)


# ── 認証ヘルパー ───────────────────────────────────────────────────────────────

def get_session_id(request: Request) -> str | None:
    return request.cookies.get("session_id")


def require_auth(request: Request):
    sid = get_session_id(request)
    if not sid or sid not in sessions:
        raise HTTPException(status_code=401, detail="認証が必要です")


# ── 認証エンドポイント ─────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: LoginRequest):
    correct = database.get_setting("app_password") or os.getenv("APP_PASSWORD", "1234")
    if body.password != correct:
        raise HTTPException(status_code=401, detail="パスワードが違います")

    sid = str(uuid.uuid4())
    sessions.add(sid)
    resp = JSONResponse({"success": True})
    resp.set_cookie("session_id", sid, httponly=True, max_age=86400 * 30, samesite="lax")
    return resp


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    sid = get_session_id(request)
    sessions.discard(sid)
    resp = JSONResponse({"success": True})
    resp.delete_cookie("session_id")
    return resp


@app.get("/api/me")
async def me(request: Request):
    require_auth(request)
    return JSONResponse({"user_name": database.get_setting("user_name") or "Serick"})


# ── チャットエンドポイント ─────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    images: list[str] = []  # base64文字列のリスト


@app.post("/api/chat")
async def chat(request: Request, body: ChatRequest):
    require_auth(request)

    return StreamingResponse(
        claude_client.stream_chat(body.message, body.images, conversation_history),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.delete("/api/chat/history")
async def clear_history(request: Request):
    require_auth(request)
    conversation_history.clear()
    return JSONResponse({"success": True})


# ── データ参照エンドポイント ───────────────────────────────────────────────────

@app.get("/api/today")
async def today(request: Request):
    require_auth(request)
    return JSONResponse(database.get_daily_summary())


# ── 静的ファイル / フロントエンド ──────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ── 起動コマンド（参考） ───────────────────────────────────────────────────────
# uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000

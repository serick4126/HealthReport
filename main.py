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
import report_generator

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


# ── 設定エンドポイント ─────────────────────────────────────────────────────────

EDITABLE_SETTINGS = {"user_name", "user_height_cm", "daily_calorie_goal", "app_password", "anthropic_api_key"}


SENSITIVE_KEYS = {"app_password", "anthropic_api_key"}


@app.get("/api/settings")
async def get_settings(request: Request):
    require_auth(request)
    plain_keys = ["user_name", "user_height_cm", "daily_calorie_goal"]
    result = {k: database.get_setting(k) or "" for k in plain_keys}
    # 機密項目は値の有無のみ返す（平文は返さない）
    for k in SENSITIVE_KEYS:
        result[k] = "set" if database.get_setting(k) else ""
    return JSONResponse(result)


class SettingsBatchRequest(BaseModel):
    settings: dict[str, str]


@app.post("/api/settings")
async def save_settings(request: Request, body: SettingsBatchRequest):
    require_auth(request)
    for key, value in body.settings.items():
        if key not in EDITABLE_SETTINGS:
            raise HTTPException(status_code=400, detail=f"編集不可のキーです: {key}")
        database.save_setting(key, value)
    return JSONResponse({"success": True})


# ── food-defaults エンドポイント ───────────────────────────────────────────────

@app.get("/api/food-defaults")
async def list_food_defaults(request: Request):
    require_auth(request)
    return JSONResponse(database.get_food_defaults())


class FoodDefaultRequest(BaseModel):
    keyword: str
    description: str
    notes: str = ""


@app.post("/api/food-defaults")
async def upsert_food_default(request: Request, body: FoodDefaultRequest):
    require_auth(request)
    database.save_food_default(body.keyword, body.description, body.notes or None)
    return JSONResponse({"success": True})


@app.delete("/api/food-defaults/{keyword}")
async def remove_food_default(request: Request, keyword: str):
    require_auth(request)
    deleted = database.delete_food_default(keyword)
    if not deleted:
        raise HTTPException(status_code=404, detail="見つかりません")
    return JSONResponse({"success": True})


# ── 履歴・集計・画像エンドポイント ────────────────────────────────────────────

@app.get("/api/history")
async def history_api(request: Request, days: int = 30, start: str = None, end: str = None):
    require_auth(request)
    return JSONResponse(database.get_history(days, start, end))


@app.get("/api/stats")
async def stats_api(request: Request, days: int = 7, start: str = None, end: str = None):
    require_auth(request)
    return JSONResponse(database.get_stats(days, start, end))


@app.get("/api/image/{meal_id}")
async def meal_image(request: Request, meal_id: int):
    require_auth(request)
    result = database.get_meal_image(meal_id)
    if not result:
        raise HTTPException(status_code=404, detail="画像が見つかりません")
    image_data, mime_type = result
    return Response(content=image_data, media_type=mime_type)


# ── 静的ファイル / フロントエンド ──────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/history")
async def history_page():
    return FileResponse(STATIC_DIR / "history.html")


@app.get("/stats")
async def stats_page():
    return FileResponse(STATIC_DIR / "stats.html")


@app.get("/settings")
async def settings_page():
    return FileResponse(STATIC_DIR / "settings.html")


# ── レポートエンドポイント ─────────────────────────────────────────────────────

@app.get("/api/report/weeks")
async def report_weeks(request: Request):
    require_auth(request)
    return JSONResponse(database.get_report_weeks())


@app.get("/api/report/preview")
async def report_preview(request: Request, start: str):
    require_auth(request)
    from datetime import date as _date, timedelta
    end = (_date.fromisoformat(start) + timedelta(days=6)).isoformat()
    data = database.get_report_data(start, end)
    charts = report_generator.generate_charts_base64(data)
    comment = await report_generator.generate_claude_comment(data)
    html = report_generator.generate_report_html(data, charts, comment)
    return Response(content=html, media_type="text/html; charset=utf-8")



@app.get("/report")
async def report_page():
    return FileResponse(STATIC_DIR / "report.html")


# ── 起動コマンド（参考） ───────────────────────────────────────────────────────
# uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000

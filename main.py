import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

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

HISTORY_KEEP_ON_SESSION_END = 10  # セッション切れ時に保持する件数


def _sanitize_loaded_history(messages: list[dict]) -> list[dict]:
    """
    DBからロードした会話履歴をClaude APIに送信できる形に修正する。
    - 先頭のassistantメッセージを削除（APIはuserメッセージから始まる必要がある）
    - 先頭のtool_resultのみのuserメッセージを削除（対応するtool_useが存在しない）
    """
    msgs = list(messages)
    while msgs:
        msg = msgs[0]
        if msg["role"] == "assistant":
            msgs.pop(0)
            continue
        if msg["role"] == "user":
            content = msg.get("content", [])
            if isinstance(content, list) and content and all(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            ):
                msgs.pop(0)
                continue
        break
    return msgs


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    # サーバー起動時：DBから直近履歴とサマリーを復元し、無効な先頭メッセージを除去
    saved = database.load_recent_conversation(HISTORY_KEEP_ON_SESSION_END)
    saved = _sanitize_loaded_history(saved)
    conversation_history.extend(saved)
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
    if sid and sid in sessions:
        # ログアウト時：直近N件のみDBに残してトリミング
        database.trim_conversation_history(HISTORY_KEEP_ON_SESSION_END)
    sessions.discard(sid)
    resp = JSONResponse({"success": True})
    resp.delete_cookie("session_id")
    return resp


@app.get("/api/me")
async def me(request: Request):
    require_auth(request)
    return JSONResponse({"user_name": database.get_setting("user_name") or "DefaultName"})


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


@app.get("/api/chat/messages")
async def get_chat_messages(request: Request):
    """チャット画面の再描画用に、表示可能なメッセージ一覧を返す"""
    require_auth(request)
    raw = database.load_recent_conversation(40)
    result = []
    for msg in raw:
        role = msg["role"]
        content = msg["content"]
        if not isinstance(content, list):
            continue
        # tool_result のみのメッセージ（ツール実行結果）は表示しない
        if all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            continue
        texts = []
        has_image = False
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                t = block.get("text", "").strip()
                if t:
                    texts.append(t)
            elif block.get("type") == "image":
                has_image = True
            # tool_use ブロックは表示しない
        text = "\n".join(texts)
        if not text and not has_image:
            continue
        result.append({"role": role, "text": text, "has_image": has_image})
    return JSONResponse(result)


@app.delete("/api/chat/history")
async def clear_history(request: Request):
    require_auth(request)
    conversation_history.clear()
    database.clear_conversation_history()
    return JSONResponse({"success": True})


# ── データ参照エンドポイント ───────────────────────────────────────────────────

@app.get("/api/today")
async def today(request: Request):
    require_auth(request)
    return JSONResponse(database.get_daily_summary())


# ── 設定エンドポイント ─────────────────────────────────────────────────────────

EDITABLE_SETTINGS = {"user_name", "user_height_cm", "daily_calorie_goal", "app_password", "anthropic_api_key", "user_notes", "savings_mode", "normal_model", "savings_model", "cache_ttl", "use_food_defaults", "auto_save_food_defaults"}


SENSITIVE_KEYS = {"app_password", "anthropic_api_key"}


@app.get("/api/settings")
async def get_settings(request: Request):
    require_auth(request)
    plain_keys = ["user_name", "user_height_cm", "daily_calorie_goal", "user_notes", "savings_mode", "normal_model", "savings_model", "cache_ttl", "use_food_defaults", "auto_save_food_defaults"]
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


# ── AIモデル一覧エンドポイント ─────────────────────────────────────────────────

@app.get("/api/models")
async def list_models(request: Request):
    """Anthropic APIから利用可能なモデル一覧を取得する"""
    require_auth(request)
    try:
        client = claude_client.get_client()
        models_page = await client.models.list()
        models = [
            {"id": m.id, "display_name": getattr(m, "display_name", m.id)}
            for m in models_page.data
        ]
        # IDでソート（新しいものが上に来るよう降順）
        models.sort(key=lambda m: m["id"], reverse=True)
        return JSONResponse({"models": models})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


@app.put("/api/food-defaults/{keyword}")
async def edit_food_default(request: Request, keyword: str, body: FoodDefaultRequest):
    require_auth(request)
    # キーワードが変わった場合は旧エントリを削除してから新規作成
    if keyword != body.keyword:
        database.delete_food_default(keyword)
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


# ── 食事記録 CRUD ──────────────────────────────────────────────────────────────

class MealUpdateRequest(BaseModel):
    meal_date: str
    meal_type: str
    description: str
    calories: Optional[int] = None
    protein: Optional[float] = None
    fat: Optional[float] = None
    carbs: Optional[float] = None
    sodium: Optional[float] = None
    notes: Optional[str] = None


@app.put("/api/meals/{meal_id}")
async def update_meal(request: Request, meal_id: int, body: MealUpdateRequest):
    require_auth(request)
    ok = database.update_meal_full(
        meal_id, body.meal_date, body.meal_type, body.description,
        body.calories, body.protein, body.fat, body.carbs, body.sodium, body.notes,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="食事記録が見つかりません")
    return JSONResponse({"success": True})


@app.delete("/api/meals/{meal_id}")
async def delete_meal(request: Request, meal_id: int):
    require_auth(request)
    ok = database.delete_meal(meal_id)
    if not ok:
        raise HTTPException(status_code=404, detail="食事記録が見つかりません")
    return JSONResponse({"success": True})


# ── 体重記録 CRUD ──────────────────────────────────────────────────────────────

class WeightUpdateRequest(BaseModel):
    weight_kg: float


class WeightCreateRequest(BaseModel):
    log_date: str
    time_of_day: str
    weight_kg: float


@app.post("/api/weight")
async def create_weight(request: Request, body: WeightCreateRequest):
    require_auth(request)
    if body.time_of_day not in ("morning", "evening"):
        raise HTTPException(status_code=422, detail="time_of_day は morning/evening のみ有効です")
    weight_id = database.save_weight(body.log_date, body.time_of_day, body.weight_kg)
    return JSONResponse({"success": True, "id": weight_id})


@app.put("/api/weight/{weight_id}")
async def update_weight(request: Request, weight_id: int, body: WeightUpdateRequest):
    require_auth(request)
    ok = database.update_weight_by_id(weight_id, body.weight_kg)
    if not ok:
        raise HTTPException(status_code=404, detail="体重記録が見つかりません")
    return JSONResponse({"success": True})


@app.delete("/api/weight/{weight_id}")
async def delete_weight(request: Request, weight_id: int):
    require_auth(request)
    ok = database.delete_weight_by_id(weight_id)
    if not ok:
        raise HTTPException(status_code=404, detail="体重記録が見つかりません")
    return JSONResponse({"success": True})


# ── 歩数記録 CRUD ──────────────────────────────────────────────────────────────

class StepsUpdateRequest(BaseModel):
    steps: int


class StepsCreateRequest(BaseModel):
    log_date: str
    steps: int


@app.post("/api/steps")
async def create_steps(request: Request, body: StepsCreateRequest):
    require_auth(request)
    result = database.save_steps(body.log_date, body.steps)
    return JSONResponse({"success": True, "id": result["id"]})


@app.put("/api/steps/{steps_id}")
async def update_steps(request: Request, steps_id: int, body: StepsUpdateRequest):
    require_auth(request)
    ok = database.update_steps_by_id(steps_id, body.steps)
    if not ok:
        raise HTTPException(status_code=404, detail="歩数記録が見つかりません")
    return JSONResponse({"success": True})


@app.delete("/api/steps/{steps_id}")
async def delete_steps(request: Request, steps_id: int):
    require_auth(request)
    ok = database.delete_steps_by_id(steps_id)
    if not ok:
        raise HTTPException(status_code=404, detail="歩数記録が見つかりません")
    return JSONResponse({"success": True})


# ── 食事画像 CRUD ──────────────────────────────────────────────────────────────

class ImageUploadRequest(BaseModel):
    image_b64: str  # base64エンコード済み画像データ


@app.get("/api/meals/{meal_id}/images")
async def list_meal_images(request: Request, meal_id: int):
    require_auth(request)
    return JSONResponse(database.get_meal_images(meal_id))


@app.post("/api/meals/{meal_id}/images")
async def add_meal_image(request: Request, meal_id: int, body: ImageUploadRequest):
    require_auth(request)
    import image_utils
    processed_b64 = image_utils.process_image_b64(body.image_b64)
    import base64
    image_bytes = base64.b64decode(processed_b64)
    image_id = database.save_meal_image(meal_id, image_bytes, "image/jpeg", "photo")
    return JSONResponse({"success": True, "image_id": image_id})


@app.get("/api/images/{image_id}")
async def get_image_by_id(request: Request, image_id: int):
    require_auth(request)
    result = database.get_meal_image_by_id(image_id)
    if not result:
        raise HTTPException(status_code=404, detail="画像が見つかりません")
    image_data, mime_type = result
    return Response(content=image_data, media_type=mime_type)


@app.delete("/api/images/{image_id}")
async def delete_image(request: Request, image_id: int):
    require_auth(request)
    ok = database.delete_meal_image(image_id)
    if not ok:
        raise HTTPException(status_code=404, detail="画像が見つかりません")
    return JSONResponse({"success": True})


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

import hmac
import json
import logging
import os
import re
import time
import uuid

logger = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Annotated, Optional

from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

import claude_client
import database
import report_generator
from image_utils import _UPLOAD_DIR as UPLOAD_DIR, save_image_to_fs

# ── セッション管理（シングルユーザー・インメモリ） ────────────────────────────

sessions: set[str] = set()

# 会話履歴（シングルユーザー・インメモリ）
conversation_history: list[dict] = []

STATIC_DIR = Path(__file__).parent / "static"

# ── ログインレート制限 ─────────────────────────────────────────────────────────
_login_attempts: dict[str, list[float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SEC = 300  # 5分間

# ── Cookie設定 ────────────────────────────────────────────────────────────────
SECURE_COOKIE = os.getenv("SECURE_COOKIE", "false").lower() == "true"

# ── バイタル値の妥当範囲（ディスパッチテーブル）────────────────────────────────
VITAL_RANGES: dict[str, tuple[float, float]] = {
    "heart_rate": (30.0, 220.0),
    "spo2": (70.0, 100.0),
}

_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _require_api_key(request: Request) -> None:
    """X-API-Key ヘッダーで external_api_key 認証。失敗時は HTTPException を送出。"""
    stored_key = database.get_setting("external_api_key") or ""
    if not stored_key:
        raise HTTPException(status_code=503, detail="外部APIキーが設定されていません")
    api_key = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(api_key, stored_key):
        raise HTTPException(status_code=401, detail="認証が必要です")


def _validate_date(date_str: str) -> None:
    """YYYY-MM-DD 形式の日付を検証。不正な場合は HTTPException(422) を送出。"""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="dateはYYYY-MM-DD形式で指定してください")


def _validate_time(time_str: str) -> None:
    """HH:MM 形式の時刻を検証。不正な場合は HTTPException(422) を送出。"""
    if not _TIME_RE.match(time_str):
        raise HTTPException(status_code=422, detail="timeはHH:MM形式で指定してください")
    h, m = int(time_str[:2]), int(time_str[3:])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HTTPException(status_code=422, detail="timeの時・分が範囲外です（時: 0-23, 分: 0-59）")


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


UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=str(Path(__file__).parent / "uploads")), name="uploads")


# ── 認証ヘルパー ───────────────────────────────────────────────────────────────

def get_session_id(request: Request) -> str | None:
    return request.cookies.get("session_id")


def require_auth(request: Request):
    if database.get_setting("password_disabled") == "true":
        return  # パスワード認証が無効化されている場合はスキップ
    sid = get_session_id(request)
    if not sid or sid not in sessions:
        raise HTTPException(status_code=401, detail="認証が必要です")


# ── 認証エンドポイント ─────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


@app.post("/api/login")
async def login(request: Request, body: LoginRequest):
    # ── S2: レート制限 ─────────────────────────────────────────────────────────
    # X-Forwarded-For（Cloudflare Tunnel等のプロキシ経由）を優先
    forwarded = request.headers.get("x-forwarded-for")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    now = time.time()
    # 直近5分以内の失敗記録のみ保持
    attempts = [t for t in _login_attempts.get(client_ip, []) if now - t < LOGIN_WINDOW_SEC]
    if len(attempts) >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(status_code=429, detail="試行回数が上限に達しました。しばらく待ってから再試行してください。")

    # ── S1: タイミング攻撃対策 ────────────────────────────────────────────────
    correct = database.get_setting("app_password") or os.getenv("APP_PASSWORD", "1234")
    if not hmac.compare_digest(body.password.encode(), correct.encode()):
        attempts.append(now)
        _login_attempts[client_ip] = attempts
        raise HTTPException(status_code=401, detail="パスワードが違います")

    # 認証成功: カウンタをクリア
    _login_attempts.pop(client_ip, None)

    sid = str(uuid.uuid4())
    sessions.add(sid)
    resp = JSONResponse({"success": True})
    # ── S3+S4: SameSite=strict、SECURE_COOKIEが有効な場合のみsecure=True ─────
    resp.set_cookie(
        "session_id", sid,
        httponly=True,
        max_age=86400 * 30,
        samesite="strict",
        secure=SECURE_COOKIE,
    )
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


# ── 集計ページウィジェットレジストリ ──────────────────────────────────────────
# 唯一の真実源。新ウィジェット追加時はここに1行追加し、stats.html の CHART_DRAW_FUNCTIONS に描画関数を1エントリ追加する。

WIDGET_REGISTRY = [
    {"id": "summary",    "label": "サマリー",     "emoji": "📋", "widget_type": "summary", "canvas_id": None,             "wrap_style": None},
    {"id": "calories",   "label": "カロリー推移", "emoji": "🔥", "widget_type": "chart",   "canvas_id": "chartCalories",  "wrap_style": None},
    {"id": "weight",     "label": "体重推移",     "emoji": "⚖️", "widget_type": "chart",   "canvas_id": "chartWeight",    "wrap_style": None},
    {"id": "steps",      "label": "歩数推移",     "emoji": "👟", "widget_type": "chart",   "canvas_id": "chartSteps",     "wrap_style": None},
    {"id": "pfc",        "label": "PFCバランス",  "emoji": "🥩", "widget_type": "chart",   "canvas_id": "chartPFC",       "wrap_style": None},
    {"id": "sleep",      "label": "睡眠ログ",     "emoji": "🛏️", "widget_type": "chart",   "canvas_id": "chartSleep",     "wrap_style": "height:180px"},
    {"id": "heart_rate", "label": "脈拍推移",     "emoji": "💓", "widget_type": "chart",   "canvas_id": "chartHeartRate", "wrap_style": None},
    {"id": "spo2",       "label": "SpO2推移",    "emoji": "🫁", "widget_type": "chart",   "canvas_id": "chartSpo2",      "wrap_style": None},
    {"id": "blood_pressure", "label": "血圧推移", "emoji": "💉", "widget_type": "chart",   "canvas_id": "chartBloodPressure", "wrap_style": None},
]

# ── 設定エンドポイント ─────────────────────────────────────────────────────────

EDITABLE_SETTINGS = {"user_name", "user_height_cm", "daily_calorie_goal", "daily_steps_goal", "app_password", "anthropic_api_key", "user_notes", "savings_mode", "normal_model", "savings_model", "cache_ttl", "use_food_defaults", "auto_save_food_defaults", "split_multiple_items", "theme", "external_api_key", "day_start_hour", "password_disabled", "user_gender", "user_birthdate", "stats_widgets", "available_models"}


SENSITIVE_KEYS = {"app_password", "anthropic_api_key", "external_api_key"}


@app.get("/api/settings")
async def get_settings(request: Request):
    require_auth(request)
    plain_keys = ["user_name", "user_height_cm", "daily_calorie_goal", "daily_steps_goal", "user_notes", "savings_mode", "normal_model", "savings_model", "cache_ttl", "use_food_defaults", "auto_save_food_defaults", "split_multiple_items", "theme", "day_start_hour", "password_disabled", "user_gender", "user_birthdate", "stats_widgets", "available_models"]
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
        if key == "day_start_hour":
            try:
                hour = int(value)
            except ValueError:
                raise HTTPException(status_code=422, detail="day_start_hourは整数で指定してください")
            if not (0 <= hour <= 23):
                raise HTTPException(status_code=422, detail="day_start_hourは0〜23の範囲で指定してください")
        if key == "stats_widgets":
            if len(value) > 10000:
                raise HTTPException(status_code=400, detail="stats_widgetsが長すぎます")
            try:
                parsed = json.loads(value)
                if not isinstance(parsed, list):
                    raise ValueError("not a list")
            except (ValueError, TypeError) as e:
                logger.error(f"stats_widgets validation failed: {e}")
                raise HTTPException(status_code=400, detail="stats_widgetsは有効なJSON配列で指定してください")
        if key == "user_gender":
            if value not in ("male", "female", ""):
                raise HTTPException(status_code=400, detail="user_genderは 'male', 'female', '' のいずれかです")
        if key == "user_birthdate" and value:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
                raise HTTPException(status_code=400, detail="user_birthdateはYYYY-MM-DD形式で入力してください")
            try:
                bd = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="user_birthdateの日付が不正です")
            today = date.today()
            calc_age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
            if not (1 <= calc_age <= 120):
                raise HTTPException(status_code=400, detail="誕生日から計算した年齢が不正です（1〜120歳の範囲で設定してください）")
        database.save_setting(key, value)
    return JSONResponse({"success": True})


# ── 集計ページウィジェットレジストリ endpoint ──────────────────────────────────

@app.get("/api/stats/widgets")
async def get_widget_registry(request: Request):
    require_auth(request)
    return JSONResponse({"widgets": WIDGET_REGISTRY})


# ── AIモデル一覧エンドポイント ─────────────────────────────────────────────────

@app.get("/api/models")
async def list_models(request: Request):
    """Anthropic APIから利用可能なモデル一覧を取得してDBにキャッシュする"""
    require_auth(request)
    try:
        client = claude_client.get_client()
        models_page = await client.models.list()
        models = [
            {"id": m.id, "display_name": getattr(m, "display_name", m.id)}
            for m in models_page.data
        ]
        models.sort(key=lambda m: m["id"], reverse=True)
        database.save_setting("available_models", json.dumps(models, ensure_ascii=False))
        return JSONResponse({"models": models})
    except Exception as e:
        logger.error("モデル一覧の取得に失敗: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="サーバー内部エラーが発生しました")


# ── food-defaults エンドポイント ───────────────────────────────────────────────

@app.get("/api/food-defaults")
async def list_food_defaults(request: Request):
    require_auth(request)
    return JSONResponse(database.get_food_defaults())


class FoodDefaultRequest(BaseModel):
    keyword: str
    description: str
    notes: str = ""
    is_favorite: Optional[bool] = None


@app.post("/api/food-defaults")
async def upsert_food_default(request: Request, body: FoodDefaultRequest):
    require_auth(request)
    database.save_food_default(body.keyword, body.description, body.notes or None, body.is_favorite)
    return JSONResponse({"success": True})


@app.put("/api/food-defaults/{keyword}")
async def edit_food_default(request: Request, keyword: str, body: FoodDefaultRequest):
    require_auth(request)
    # キーワードが変わった場合は旧エントリを削除してから新規作成
    if keyword != body.keyword:
        database.delete_food_default(keyword)
    database.save_food_default(body.keyword, body.description, body.notes or None, body.is_favorite)
    return JSONResponse({"success": True})


@app.delete("/api/food-defaults/{keyword}")
async def remove_food_default(request: Request, keyword: str):
    require_auth(request)
    deleted = database.delete_food_default(keyword)
    if not deleted:
        raise HTTPException(status_code=404, detail="見つかりません")
    return JSONResponse({"success": True})


@app.post("/api/food-defaults/{keyword}/favorite")
async def toggle_favorite(request: Request, keyword: str):
    require_auth(request)
    result = database.toggle_food_default_favorite(keyword)
    if result is None:
        raise HTTPException(status_code=404, detail="見つかりません")
    return JSONResponse({"is_favorite": result})


@app.get("/api/quick-entries")
async def get_quick_entries(request: Request):
    require_auth(request)
    favorites = database.get_favorite_food_defaults()
    frequent = database.get_frequent_meals(days=30, limit=10)
    return JSONResponse({"favorites": favorites, "frequent": frequent})


# ── 履歴・集計・画像エンドポイント ────────────────────────────────────────────

@app.get("/api/history")
async def history_api(request: Request, days: int = 30, start: str = None, end: str = None):
    require_auth(request)
    return JSONResponse(database.get_history(days, start, end))


@app.get("/api/history/search")
async def search_history(request: Request, q: str = ""):
    require_auth(request)
    if not q.strip():
        return JSONResponse([])
    return JSONResponse(database.search_meals(q.strip()))


@app.get("/api/stats")
async def stats_api(request: Request, days: int = 7, start: str = None, end: str = None):
    require_auth(request)
    return JSONResponse(database.get_stats(days, start, end))


@app.get("/api/image/{meal_id}")
async def meal_image(request: Request, meal_id: int):
    from fastapi.responses import RedirectResponse
    require_auth(request)
    row = database.get_meal_image(meal_id)
    if not row:
        raise HTTPException(status_code=404, detail="画像が見つかりません")
    if row["image_path"]:
        return RedirectResponse(url=f"/{row['image_path']}", status_code=302)
    if row["image_data"]:
        return Response(content=row["image_data"], media_type=row["mime_type"])
    raise HTTPException(status_code=404, detail="画像データがありません")


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
    images = database.get_meal_images(meal_id)
    ok = database.delete_meal(meal_id)
    if not ok:
        raise HTTPException(status_code=404, detail="食事記録が見つかりません")
    for img in images:
        if img.get("image_path"):
            try:
                Path(img["image_path"]).unlink(missing_ok=True)
            except Exception:
                logger.warning("画像ファイルの削除に失敗 (path=%s)", img["image_path"], exc_info=True)
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


# ── 血圧記録 CRUD ──────────────────────────────────────────────────────────────

def _validate_blood_pressure(systolic: int, diastolic: int) -> None:
    """血圧値のバリデーション。違反時は HTTPException(422) を送出。"""
    if not (50 <= systolic <= 300):
        raise HTTPException(status_code=422, detail="systolicは50〜300の範囲で指定してください")
    if not (30 <= diastolic <= 200):
        raise HTTPException(status_code=422, detail="diastolicは30〜200の範囲で指定してください")


class BloodPressureCreateRequest(BaseModel):
    log_date: str
    time_of_day: str
    systolic: int
    diastolic: int


class BloodPressureUpdateRequest(BaseModel):
    systolic: int
    diastolic: int


@app.post("/api/blood-pressure")
async def create_blood_pressure(request: Request, body: BloodPressureCreateRequest):
    require_auth(request)
    if body.time_of_day not in ("morning", "evening"):
        raise HTTPException(status_code=422, detail="time_of_dayはmorning/eveningのみ有効です")
    _validate_date(body.log_date)
    _validate_blood_pressure(body.systolic, body.diastolic)
    bp_id = database.save_blood_pressure(body.log_date, body.time_of_day, body.systolic, body.diastolic)
    return JSONResponse({"success": True, "id": bp_id})


@app.put("/api/blood-pressure/{bp_id}")
async def update_blood_pressure(request: Request, bp_id: int, body: BloodPressureUpdateRequest):
    require_auth(request)
    _validate_blood_pressure(body.systolic, body.diastolic)
    ok = database.update_blood_pressure_by_id(bp_id, body.systolic, body.diastolic)
    if not ok:
        raise HTTPException(status_code=404, detail="血圧記録が見つかりません")
    return JSONResponse({"success": True})


@app.delete("/api/blood-pressure/{bp_id}")
async def delete_blood_pressure(request: Request, bp_id: int):
    require_auth(request)
    ok = database.delete_blood_pressure_by_id(bp_id)
    if not ok:
        raise HTTPException(status_code=404, detail="血圧記録が見つかりません")
    return JSONResponse({"success": True})


# ── 運動ログ CRUD ──────────────────────────────────────────────────────────────

_JST = timezone(timedelta(hours=9))


class ExerciseRequest(BaseModel):
    log_date: str
    calories_burned: int
    description: str = ""


class ExerciseUpdateRequest(BaseModel):
    calories_burned: int
    description: str = ""
    log_date: str = ""


def _validate_calories_and_desc(calories_burned: int, description: str) -> None:
    """calories_burned・description のバリデーション。違反時は HTTPException を送出。"""
    if not (0 <= calories_burned <= 9999):
        raise HTTPException(status_code=422, detail="calories_burned は 0〜9999 の整数を指定してください")
    if len(description) > 500:
        raise HTTPException(status_code=422, detail="description は 500 文字以内で指定してください")


def _validate_exercise_body(log_date: str, calories_burned: int, description: str) -> None:
    """運動ログ入力のバリデーション（日付＋calories＋description）。違反時は HTTPException を送出。"""
    try:
        parsed = datetime.strptime(log_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="log_date は YYYY-MM-DD 形式で指定してください")
    if parsed > datetime.now(_JST).date():
        raise HTTPException(status_code=422, detail="未来の日付は登録できません")
    _validate_calories_and_desc(calories_burned, description)


@app.post("/api/exercise")
async def create_exercise(request: Request, body: ExerciseRequest):
    require_auth(request)
    _validate_exercise_body(body.log_date, body.calories_burned, body.description)
    try:
        eid = database.save_exercise(body.log_date, body.calories_burned, body.description, source="manual")
    except Exception:
        logger.error("運動ログの保存に失敗しました", exc_info=True)
        raise HTTPException(status_code=500, detail="運動ログの保存に失敗しました")
    return JSONResponse({"success": True, "id": eid})


@app.put("/api/exercise/{exercise_id}")
async def update_exercise(request: Request, exercise_id: int, body: ExerciseUpdateRequest):
    require_auth(request)
    _validate_calories_and_desc(body.calories_burned, body.description)
    if body.log_date:
        _validate_exercise_body(body.log_date, body.calories_burned, body.description)
    try:
        ok = database.update_exercise_by_id(
            exercise_id, body.calories_burned, body.description, body.log_date
        )
    except Exception:
        logger.error("運動ログの更新に失敗しました id=%s", exercise_id, exc_info=True)
        raise HTTPException(status_code=500, detail="運動ログの更新に失敗しました")
    if not ok:
        raise HTTPException(status_code=404, detail="運動ログが見つかりません")
    return JSONResponse({"success": True})


@app.delete("/api/exercise/{exercise_id}")
async def delete_exercise(request: Request, exercise_id: int):
    require_auth(request)
    try:
        ok = database.delete_exercise_by_id(exercise_id)
    except Exception:
        logger.error("運動ログの削除に失敗しました id=%s", exercise_id, exc_info=True)
        raise HTTPException(status_code=500, detail="運動ログの削除に失敗しました")
    if not ok:
        raise HTTPException(status_code=404, detail="運動ログが見つかりません")
    return JSONResponse({"deleted": True, "id": exercise_id})


# ── 歩数受付API（iPhoneショートカット連携） ────────────────────────────────────


class StepsIngestRequest(BaseModel):
    steps: int
    date: str  # "YYYY-MM-DD"


class WeightRecord(BaseModel):
    recorded_at: str  # "YYYY/MM/DD HH:MM"
    weight: float


class WeightIngestRequest(BaseModel):
    records: list[WeightRecord]


from database import SKIP_MEAL_TYPES  # database.py の定義を正とする


class MealSkipRequest(BaseModel):
    meal_date: str
    meal_type: str


@app.post("/api/steps/ingest")
async def steps_ingest(request: Request, body: StepsIngestRequest):
    """iPhoneショートカット等の外部クライアントから歩数を受け付けるAPI。
    セッション認証不要。external_api_key（体重APIと共用）によるBearerトークン認証を使用する。"""
    # 1. APIキー取得
    stored_key = database.get_setting("external_api_key") or ""
    if not stored_key:
        raise HTTPException(status_code=503, detail="外部APIキーが設定されていません")

    # 2. 認証（タイミング攻撃対策）
    auth_header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        raise HTTPException(status_code=401, detail="認証が必要です")
    provided_key = auth_header[len(prefix):]
    if not hmac.compare_digest(provided_key.encode(), stored_key.encode()):
        raise HTTPException(status_code=401, detail="APIキーが正しくありません")

    # 3. バリデーション
    if body.steps < 1:
        raise HTTPException(status_code=422, detail="stepsは1以上の整数を指定してください")
    try:
        log_date = datetime.strptime(body.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="dateはYYYY-MM-DD形式で指定してください")
    today = datetime.now(_JST).date()
    if log_date > today:
        raise HTTPException(status_code=422, detail="未来の日付は登録できません")

    # 4. 保存（同日レコードがあれば上書き）
    result = database.save_steps(body.date, body.steps)
    return JSONResponse({
        "success": True,
        "date": body.date,
        "steps": body.steps,
        "updated": result["updated"],
    })


# ── 運動受付API（iPhoneショートカット連携） ────────────────────────────────────

class ExerciseIngestRequest(BaseModel):
    date: str            # "YYYY-MM-DD"
    calories_burned: int  # HealthKit アクティブエネルギー (kcal)
    description: str = "iOSショートカット"


@app.post("/api/exercise/ingest")
async def exercise_ingest(request: Request, body: ExerciseIngestRequest):
    """iPhoneショートカット等から消費カロリー（アクティブエネルギー）を受け付けるAPI。
    セッション認証不要。external_api_key（歩数・体重APIと共用）による Bearer トークン認証。
    iOS ショートカット設定: POST {host}/api/exercise/ingest
    ヘッダー: Authorization: Bearer {external_api_key}
    ボディ: {"date":"YYYY-MM-DD","calories_burned":{アクティブエネルギー整数},"description":"iOSショートカット"}
    """
    # 1. APIキー認証
    stored_key = database.get_setting("external_api_key") or ""
    if not stored_key:
        raise HTTPException(status_code=503, detail="外部APIキーが設定されていません")
    auth_header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        raise HTTPException(status_code=401, detail="認証が必要です")
    provided_key = auth_header[len(prefix):]
    if not hmac.compare_digest(provided_key.encode(), stored_key.encode()):
        raise HTTPException(status_code=401, detail="APIキーが正しくありません")

    # 2. バリデーション
    if not (0 <= body.calories_burned <= 9999):
        raise HTTPException(status_code=422, detail="calories_burned は 0〜9999 の整数を指定してください")
    if len(body.description) > 500:
        raise HTTPException(status_code=422, detail="description は 500 文字以内で指定してください")
    try:
        log_date = datetime.strptime(body.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="date は YYYY-MM-DD 形式で指定してください")
    today = datetime.now(_JST).date()
    if log_date > today:
        raise HTTPException(status_code=422, detail="未来の日付は登録できません")

    # 3. 保存
    try:
        eid = database.save_exercise(body.date, body.calories_burned, body.description, source="api")
    except Exception:
        logger.error("運動ログ(ingest)の保存に失敗しました", exc_info=True)
        raise HTTPException(status_code=500, detail="運動ログの保存に失敗しました")
    return JSONResponse({
        "success": True,
        "id": eid,
        "date": body.date,
        "calories_burned": body.calories_burned,
    })


# ── 体重受付API（iPhoneショートカット連携） ────────────────────────────────────

def _classify_weight_record(recorded_at_str: str, day_start_hour: int = 4) -> tuple[str, str]:
    """recorded_at（"YYYY/MM/DD HH:MM"）を (log_date, time_of_day) に変換。
    day_start_hour より前（0:00〜(day_start_hour-1):59）は前日の evening として扱う。"""
    dt = datetime.strptime(recorded_at_str, "%Y/%m/%d %H:%M")
    if dt.hour < day_start_hour:
        logical_date = (dt - timedelta(days=1)).date()
        return str(logical_date), "evening"
    elif dt.hour < 12:
        return str(dt.date()), "morning"
    else:
        return str(dt.date()), "evening"


def _select_best_weight_records(
    records: list[WeightRecord],
) -> dict[tuple[str, str], WeightRecord]:
    """同一 (log_date, time_of_day) バケット内で最も遅い recorded_at のレコードを選択。
    戻り値: {(log_date, time_of_day): record}"""
    raw_hour = database.get_setting("day_start_hour") or "4"
    try:
        day_start = int(raw_hour)
    except ValueError:
        logger.warning("day_start_hour の設定値が不正です: %s。デフォルト値 4 を使用します。", raw_hour)
        day_start = 4
    best: dict[tuple[str, str], tuple[datetime, WeightRecord]] = {}
    for rec in records:
        key = _classify_weight_record(rec.recorded_at, day_start)
        dt = datetime.strptime(rec.recorded_at, "%Y/%m/%d %H:%M")
        if key not in best or dt > best[key][0]:
            best[key] = (dt, rec)
    return {k: v[1] for k, v in best.items()}


@app.post("/api/weight/ingest")
async def weight_ingest(request: Request, body: WeightIngestRequest):
    """iPhoneショートカット等から体重レコードを受け付けるAPI。
    セッション認証不要。external_api_key（歩数APIと共用）によるBearerトークン認証を使用する。"""
    # 1. APIキー認証
    stored_key = database.get_setting("external_api_key") or ""
    if not stored_key:
        raise HTTPException(status_code=503, detail="外部APIキーが設定されていません")
    auth_header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        raise HTTPException(status_code=401, detail="認証が必要です")
    provided_key = auth_header[len(prefix):]
    if not hmac.compare_digest(provided_key.encode(), stored_key.encode()):
        raise HTTPException(status_code=401, detail="APIキーが正しくありません")

    # 2. バリデーション
    if not body.records:
        raise HTTPException(status_code=422, detail="recordsが空です")
    if len(body.records) > 100:
        raise HTTPException(status_code=422, detail="一度に送信できるレコードは100件以内です")
    for rec in body.records:
        try:
            datetime.strptime(rec.recorded_at, "%Y/%m/%d %H:%M")
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"recorded_at の形式が不正です: {rec.recorded_at}（YYYY/MM/DD HH:MM形式で指定）",
            )
        if not (20.0 <= rec.weight <= 300.0):
            raise HTTPException(
                status_code=422,
                detail=f"weight は 20〜300 の範囲で指定してください: {rec.weight}",
            )

    # 3. 分類・重複排除・保存
    today = datetime.now(_JST).date()
    best_records = _select_best_weight_records(body.records)
    saved = []
    try:
        for (log_date_str, time_of_day), rec in best_records.items():
            log_date = date.fromisoformat(log_date_str)
            if log_date > today:
                logger.warning("未来の日付をスキップ: %s", log_date_str)
                continue
            result = database.upsert_weight(log_date_str, time_of_day, rec.weight)
            saved.append({
                "log_date": log_date_str,
                "time_of_day": time_of_day,
                "weight_kg": rec.weight,
                "updated": result["updated"],
            })
    except Exception:
        logger.error("体重データの保存中にエラーが発生しました", exc_info=True)
        raise HTTPException(status_code=500, detail="体重データの保存に失敗しました")

    return JSONResponse({"success": True, "saved": saved})


# ── 血圧受付API（iPhoneショートカット連携） ────────────────────────────────────

class BloodPressureRecord(BaseModel):
    recorded_at: str  # "YYYY/MM/DD HH:MM"
    systolic: int
    diastolic: int


class BloodPressureIngestRequest(BaseModel):
    records: list[BloodPressureRecord]


@app.post("/api/external/blood-pressure")
async def blood_pressure_ingest(request: Request, body: BloodPressureIngestRequest):
    """iPhoneショートカット等から血圧を受け付けるAPI。
    セッション認証不要。external_api_key（歩数・体重と共用）による Bearer トークン認証。
    ヘッダー: Authorization: Bearer {external_api_key}
    ボディ: {"records": [{"recorded_at": "YYYY/MM/DD HH:MM", "systolic": 120, "diastolic": 80}]}
    """
    # 1. APIキー認証
    stored_key = database.get_setting("external_api_key") or ""
    if not stored_key:
        raise HTTPException(status_code=503, detail="外部APIキーが設定されていません")
    auth_header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        raise HTTPException(status_code=401, detail="認証が必要です")
    provided_key = auth_header[len(prefix):]
    if not hmac.compare_digest(provided_key.encode(), stored_key.encode()):
        raise HTTPException(status_code=401, detail="APIキーが正しくありません")

    # 2. バリデーション
    if not body.records:
        raise HTTPException(status_code=422, detail="recordsが空です")
    if len(body.records) > 100:
        raise HTTPException(status_code=422, detail="一度に送信できるレコードは100件以内です")
    for rec in body.records:
        try:
            datetime.strptime(rec.recorded_at, "%Y/%m/%d %H:%M")
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"recorded_at の形式が不正です: {rec.recorded_at}（YYYY/MM/DD HH:MM形式で指定）",
            )
        _validate_blood_pressure(rec.systolic, rec.diastolic)

    # 3. 分類・保存（後着優先 upsert）
    raw_hour = database.get_setting("day_start_hour") or "4"
    try:
        day_start = int(raw_hour)
    except ValueError:
        day_start = 4
    today = datetime.now(_JST).date()
    saved = []
    try:
        for rec in body.records:
            log_date_str, time_of_day = _classify_weight_record(rec.recorded_at, day_start)
            log_date = date.fromisoformat(log_date_str)
            if log_date > today:
                logger.warning("未来の日付をスキップ: %s", log_date_str)
                continue
            result = database.upsert_blood_pressure(log_date_str, time_of_day, rec.systolic, rec.diastolic)
            saved.append({
                "log_date": log_date_str,
                "time_of_day": time_of_day,
                "systolic": rec.systolic,
                "diastolic": rec.diastolic,
                "updated": result["updated"],
            })
    except Exception:
        logger.error("血圧データの保存中にエラーが発生しました", exc_info=True)
        raise HTTPException(status_code=500, detail="血圧データの保存に失敗しました")

    return JSONResponse({"success": True, "saved": saved})


# ── 食事スキップ CRUD ──────────────────────────────────────────────────────────

def _validate_skip_request(meal_date: str, meal_type: str):
    """スキップAPIの入力バリデーション。不正な場合 HTTPException を raise。"""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", meal_date):
        raise HTTPException(status_code=422, detail="meal_date は YYYY-MM-DD 形式で指定してください")
    try:
        datetime.strptime(meal_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="meal_date が不正な日付です")
    if meal_type not in SKIP_MEAL_TYPES:
        raise HTTPException(
            status_code=422,
            detail="meal_type は breakfast/lunch/dinner のいずれかで指定してください",
        )


@app.get("/api/meal-skips")
async def get_meal_skips(request: Request, date: str):
    require_auth(request)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=422, detail="date は YYYY-MM-DD 形式で指定してください")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date が不正な日付です")
    try:
        skipped = database.get_meal_skips_by_date(date)
        return JSONResponse({"skipped": skipped})
    except Exception:
        logger.error("スキップデータ取得中にエラーが発生しました", exc_info=True)
        raise HTTPException(status_code=500, detail="データの取得に失敗しました")


@app.post("/api/meal-skips")
async def add_meal_skip(request: Request, body: MealSkipRequest):
    require_auth(request)
    _validate_skip_request(body.meal_date, body.meal_type)
    try:
        database.save_meal_skip(body.meal_date, body.meal_type)
        return JSONResponse({"success": True, "skipped": True})
    except Exception:
        logger.error("スキップ記録中にエラーが発生しました", exc_info=True)
        raise HTTPException(status_code=500, detail="スキップの記録に失敗しました")


@app.delete("/api/meal-skips")
async def remove_meal_skip(request: Request, body: MealSkipRequest):
    require_auth(request)
    _validate_skip_request(body.meal_date, body.meal_type)
    try:
        database.delete_meal_skip(body.meal_date, body.meal_type)
        return JSONResponse({"success": True, "skipped": False})
    except Exception:
        logger.error("スキップ削除中にエラーが発生しました", exc_info=True)
        raise HTTPException(status_code=500, detail="スキップの削除に失敗しました")


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
    import base64
    import image_utils

    # Base64サイズ上限チェック（10MB）
    try:
        raw_bytes = base64.b64decode(body.image_b64)
    except Exception:
        raise HTTPException(status_code=422, detail="画像データのBase64デコードに失敗しました")
    if len(raw_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="画像サイズが上限(10MB)を超えています")

    try:
        processed_b64 = image_utils.process_image_b64(body.image_b64)
        image_bytes = base64.b64decode(processed_b64)
    except Exception:
        logger.error("画像処理に失敗しました", exc_info=True)
        raise HTTPException(status_code=422, detail="画像の処理に失敗しました")

    # DBには image_path のみ記録（BLOB保存なし）
    rel_path = save_image_to_fs(image_bytes)
    image_id = database.save_meal_image_path(meal_id, rel_path, "image/jpeg", "photo")
    return JSONResponse({"success": True, "image_id": image_id})


@app.get("/api/images/{image_id}")
async def get_image_by_id(request: Request, image_id: int):
    from fastapi.responses import RedirectResponse
    require_auth(request)
    row = database.get_meal_image_by_id(image_id)
    if not row:
        raise HTTPException(status_code=404, detail="画像が見つかりません")
    if row["image_path"]:
        return RedirectResponse(url=f"/{row['image_path']}", status_code=302)
    if row["image_data"]:
        return Response(content=row["image_data"], media_type=row["mime_type"])
    raise HTTPException(status_code=404, detail="画像データがありません")


@app.delete("/api/images/{image_id}")
async def delete_image(request: Request, image_id: int):
    require_auth(request)
    row = database.get_meal_image_by_id(image_id)
    if not row:
        raise HTTPException(status_code=404, detail="画像が見つかりません")
    ok = database.delete_meal_image(image_id)
    if not ok:
        raise HTTPException(status_code=404, detail="画像が見つかりません")
    if row["image_path"]:
        try:
            Path(row["image_path"]).unlink(missing_ok=True)
        except Exception:
            logger.warning("画像ファイルの削除に失敗 (path=%s)", row["image_path"], exc_info=True)
    return JSONResponse({"success": True})


# ── 画像BLOBマイグレーション（管理者用） ───────────────────────────────────────

@app.post("/api/admin/migrate-images")
async def migrate_images(request: Request):
    """既存BLOBデータをファイルシステムへ移行する（管理者API）。
    X-API-Key ヘッダーで external_api_key 認証が必要。
    """
    _require_api_key(request)

    from datetime import datetime as _dt
    migrated = 0
    skipped = 0
    errors = 0
    batch_size = 100
    failed_ids: set[int] = set()  # エラー済みIDを除外して無限ループを防止

    while True:
        rows = database.get_images_without_path(limit=batch_size)
        rows = [r for r in rows if r["id"] not in failed_ids]
        if not rows:
            break
        for row in rows:
            if not row["image_data"]:
                skipped += 1
                continue
            try:
                now = _dt.now()
                sub_dir = UPLOAD_DIR / f"{now.year:04d}" / f"{now.month:02d}"
                sub_dir.mkdir(parents=True, exist_ok=True)
                ext = "jpg" if row["mime_type"] == "image/jpeg" else "bin"
                filename = f"{uuid.uuid4()}.{ext}"
                file_path = sub_dir / filename
                file_path.write_bytes(row["image_data"])
                rel_path = file_path.relative_to(UPLOAD_DIR.parent.parent).as_posix()
                database.update_meal_image_path(row["id"], rel_path)
                migrated += 1
            except Exception:
                logger.error(f"migrate-images: image_id={row['id']} の移行に失敗", exc_info=True)
                failed_ids.add(row["id"])
                errors += 1

    return JSONResponse({"migrated": migrated, "skipped": skipped, "errors": errors})


# ── 睡眠ログ API ───────────────────────────────────────────────────────────────

class SleepIngestRequest(BaseModel):
    date: str
    sleep_start: str
    sleep_end: str
    deep_minutes: Optional[int] = None
    rem_minutes: Optional[int] = None
    awake_minutes: Optional[int] = None
    source: str = "healthkit"


@app.post("/api/sleep/ingest", status_code=201)
async def sleep_ingest(request: Request, body: SleepIngestRequest):
    """Apple Watch 等から睡眠ログを受け付けるAPI。X-API-Key 認証必須。"""
    _require_api_key(request)
    _validate_date(body.date)
    _validate_time(body.sleep_start)
    _validate_time(body.sleep_end)
    if body.source not in ("healthkit", "manual"):
        raise HTTPException(status_code=422, detail="sourceはhealthkitまたはmanualを指定してください")
    for field_name in ("deep_minutes", "rem_minutes", "awake_minutes"):
        val = getattr(body, field_name)
        if val is not None and not (0 <= val <= 1440):
            raise HTTPException(status_code=422, detail=f"{field_name}は0〜1440の範囲で指定してください")
    result = database.upsert_sleep_log(
        body.date, body.sleep_start, body.sleep_end,
        body.deep_minutes, body.rem_minutes, body.awake_minutes,
        body.source,
    )
    return JSONResponse(
        {"success": True, "date": body.date, "duration_minutes": result["duration_minutes"]},
        status_code=201,
    )


@app.get("/api/sleep/summary")
async def sleep_summary(request: Request, start_date: str, end_date: str):
    """指定期間の睡眠ログを返す。"""
    require_auth(request)
    _validate_date(start_date)
    _validate_date(end_date)
    logs = database.get_sleep_logs(start_date, end_date)
    return JSONResponse({"logs": logs})


# ── バイタルログ API ───────────────────────────────────────────────────────────

class VitalIngestRequest(BaseModel):
    date: str
    type: str
    value: float
    time: Optional[str] = None
    source: str = "healthkit"


class VitalAlertRequest(BaseModel):
    date: str
    time: Optional[str] = None
    note: Optional[str] = Field(None, max_length=1000)


@app.post("/api/vitals/ingest", status_code=201)
async def vitals_ingest(request: Request, body: VitalIngestRequest):
    """バイタルログ（脈拍・SpO2）を受け付けるAPI。X-API-Key 認証必須。"""
    _require_api_key(request)
    _validate_date(body.date)
    if body.time:
        _validate_time(body.time)
    if body.type not in VITAL_RANGES:
        raise HTTPException(status_code=422, detail="typeはheart_rateまたはspo2を指定してください")
    low, high = VITAL_RANGES[body.type]
    if not (low <= body.value <= high):
        raise HTTPException(
            status_code=422,
            detail=f"valueが範囲外です（{body.type}: {low}〜{high}）",
        )
    vid = database.insert_vital_log(body.date, body.type, body.value, body.time, source=body.source)
    return JSONResponse({"success": True, "id": vid}, status_code=201)


@app.post("/api/vitals/alert", status_code=201)
async def vitals_alert(request: Request, body: VitalAlertRequest):
    """高血圧パターン通知を受け付けるAPI。X-API-Key 認証必須。"""
    _require_api_key(request)
    _validate_date(body.date)
    if body.time:
        _validate_time(body.time)
    vid = database.insert_vital_log(body.date, "bp_alert", note=body.note, time=body.time)
    return JSONResponse({"success": True, "id": vid}, status_code=201)


@app.get("/api/vitals/summary")
async def vitals_summary(
    request: Request,
    start_date: str,
    end_date: str,
    type: Optional[str] = None,
):
    """指定期間のバイタルログを返す。type 指定で絞り込み可能。"""
    require_auth(request)
    _validate_date(start_date)
    _validate_date(end_date)
    logs = database.get_vital_logs(start_date, end_date, type)
    return JSONResponse({"logs": logs})


# ── BMI・基礎代謝 API ──────────────────────────────────────────────────────────

@app.get("/api/bmi")
async def bmi_info(request: Request):
    """最新体重と user_height_cm 設定から BMI・基礎代謝を返す。"""
    require_auth(request)
    height_str = database.get_setting("user_height_cm") or ""
    try:
        height_cm = float(height_str) if height_str else 0.0
    except ValueError:
        height_cm = 0.0
    if height_cm <= 0:
        raise HTTPException(status_code=404, detail="height_not_configured")

    info = database.get_latest_bmi_info()
    if info is None:
        raise HTTPException(status_code=404, detail="no_weight_record")
    return JSONResponse({
        "bmi": info["bmi"],
        "bmi_status": info["bmi_status"],
        "bmr_kcal": info["bmr_kcal"],
        "bmr_note": info["bmr_note"],
        "weight_kg": info["weight_kg"],
        "height_cm": info["height_cm"],
        "log_date": info["log_date"],
    })


# ── 食事時刻 API ───────────────────────────────────────────────────────────────

class MealTimeRequest(BaseModel):
    meal_time: Optional[str] = None  # "HH:MM" or null（削除）


@app.patch("/api/meals/{meal_id}/time")
async def update_meal_time(request: Request, meal_id: int, body: MealTimeRequest):
    """食事時刻を更新する（meal_time=null で削除）。"""
    require_auth(request)
    if body.meal_time is not None:
        _validate_time(body.meal_time)
    ok = database.update_meal(meal_id, meal_time=body.meal_time)
    if not ok:
        raise HTTPException(status_code=404, detail="食事記録が見つかりません")
    return JSONResponse({"success": True, "meal_time": body.meal_time})


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
    prev_week = database.get_report_data_previous_week(start)
    charts = report_generator.generate_charts_base64(data)
    comment = await report_generator.generate_claude_comment(data, prev_week=prev_week)
    html = report_generator.generate_report_html(data, charts, comment)
    return Response(content=html, media_type="text/html; charset=utf-8")



@app.get("/report")
async def report_page():
    return FileResponse(STATIC_DIR / "report.html")


# ── 起動コマンド（参考） ───────────────────────────────────────────────────────
# uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000

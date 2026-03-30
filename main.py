import hmac
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

# ── ログインレート制限 ─────────────────────────────────────────────────────────
_login_attempts: dict[str, list[float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SEC = 300  # 5分間

# ── Cookie設定 ────────────────────────────────────────────────────────────────
SECURE_COOKIE = os.getenv("SECURE_COOKIE", "false").lower() == "true"


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


UPLOAD_DIR = Path(__file__).parent / "uploads" / "meal_images"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=str(Path(__file__).parent / "uploads")), name="uploads")


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


# ── 設定エンドポイント ─────────────────────────────────────────────────────────

EDITABLE_SETTINGS = {"user_name", "user_height_cm", "daily_calorie_goal", "app_password", "anthropic_api_key", "user_notes", "savings_mode", "normal_model", "savings_model", "cache_ttl", "use_food_defaults", "auto_save_food_defaults", "split_multiple_items", "theme", "external_api_key"}


SENSITIVE_KEYS = {"app_password", "anthropic_api_key", "external_api_key"}


@app.get("/api/settings")
async def get_settings(request: Request):
    require_auth(request)
    plain_keys = ["user_name", "user_height_cm", "daily_calorie_goal", "user_notes", "savings_mode", "normal_model", "savings_model", "cache_ttl", "use_food_defaults", "auto_save_food_defaults", "split_multiple_items", "theme"]
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


# ── 歩数受付API（iPhoneショートカット連携） ────────────────────────────────────

_JST = timezone(timedelta(hours=9))


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


# ── 体重受付API（iPhoneショートカット連携） ────────────────────────────────────

def _classify_weight_record(recorded_at_str: str) -> tuple[str, str]:
    """recorded_at（"YYYY/MM/DD HH:MM"）を (log_date, time_of_day) に変換。
    0:00-3:59 は前日の evening として扱う（4:00 が日の境界）。"""
    dt = datetime.strptime(recorded_at_str, "%Y/%m/%d %H:%M")
    if dt.hour < 4:
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
    best: dict[tuple[str, str], tuple[datetime, WeightRecord]] = {}
    for rec in records:
        key = _classify_weight_record(rec.recorded_at)
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

    # YYYY/MM/ サブディレクトリにUUIDファイル名で保存
    from datetime import datetime as _dt
    now = _dt.now()
    sub_dir = UPLOAD_DIR / f"{now.year:04d}" / f"{now.month:02d}"
    sub_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4()}.jpg"
    file_path = sub_dir / filename
    file_path.write_bytes(image_bytes)

    # DBには image_path のみ記録（BLOB保存なし）
    rel_path = file_path.relative_to(UPLOAD_DIR.parent.parent).as_posix()
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
    ok = database.delete_meal_image(image_id)
    if not ok:
        raise HTTPException(status_code=404, detail="画像が見つかりません")
    return JSONResponse({"success": True})


# ── 画像BLOBマイグレーション（管理者用） ───────────────────────────────────────

@app.post("/api/admin/migrate-images")
async def migrate_images(request: Request):
    """既存BLOBデータをファイルシステムへ移行する（管理者API）。
    X-API-Key ヘッダーで external_api_key 認証が必要。
    """
    api_key = request.headers.get("x-api-key", "")
    stored_key = database.get_setting("external_api_key") or ""
    if not stored_key or not hmac.compare_digest(api_key, stored_key):
        raise HTTPException(status_code=401, detail="認証が必要です")

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

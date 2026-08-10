"""MCP サーバー定義。

claude.ai（Web版）カスタムコネクタから HealthReport の記録操作を提供する。
推論はクライアント側 AI が担い、本サーバーは Anthropic API を呼ばない（設計仕様書 §1.4）。
"""
import functools
import inspect
import json
import logging
from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import database
import record_service

logger = logging.getLogger(__name__)

# ツール引数 description の共通文言（P-12: ツール間で表記を統一する）
_DESC_DATE = "YYYY-MM-DD形式。原則省略する。ユーザーが明示的に過去日を指定した場合のみ渡す"
_DESC_MEAL_TYPE = "breakfast / lunch / dinner / snack / late_night のいずれか"
_DESC_MEAL_TIME = "HH:MM形式。省略時は当日なら現在時刻、過去日ならNULL"
_DESC_ITEMS = "登録する品目の配列（1件以上）。各要素は description / calories / protein / fat / carbs / sodium / notes"
_DESC_MEAL_ID = "更新対象の食事記録ID（get_daily_summary で確認できる）"

# バリデーション上限（description 文言と同期させる）
_MAX_DESC = 500
_MAX_NOTES = 1000
_MAX_NUMERIC = 9999

_DESC_MEAL_FIELDS = {
    "description": f"品目名。最大{_MAX_DESC}文字",
    "calories": f"カロリー(kcal)。0〜{_MAX_NUMERIC}の整数。不明なら省略する",
    "protein": f"タンパク質(g)。0〜{_MAX_NUMERIC}の数値。不明なら省略する",
    "fat": f"脂質(g)。0〜{_MAX_NUMERIC}の数値。不明なら省略する",
    "carbs": f"炭水化物(g)。0〜{_MAX_NUMERIC}の数値。不明なら省略する",
    "sodium": f"食塩相当量(g)。0〜{_MAX_NUMERIC}の数値。不明なら省略する",
    "notes": f"メモ。最大{_MAX_NOTES}文字",
}

_NUTRIENT_NAMES = {
    "protein": "タンパク質(g)",
    "fat": "脂質(g)",
    "carbs": "炭水化物(g)",
    "sodium": "食塩相当量(g)",
}

# P-12: 単値系ツールの共通引数 description（7ツール間で文言を完全に統一する）
_DESC_TIME_OF_DAY = "morning（朝）または evening（夜）"
_DESC_SKIP_MEAL_TYPE = "breakfast / lunch / dinner のいずれか"
_DESC_MODE = "replace（置換・既定）または append（追記）"
_DESC_SKIPPED = "true=スキップ設定 / false=解除"
_DESC_MEMO_TEXT = "メモ本文。置換は2000文字・追記は500文字以内"

# 単値系ツールの数値範囲（description 文言とバリデーションを同期させる）
_WEIGHT_MIN = 20
_WEIGHT_MAX = 300
_STEPS_MIN = 0
_STEPS_MAX = 999999
_BODY_FAT_MIN = 1.0
_BODY_FAT_MAX = 80.0
_SYSTOLIC_MIN = 50
_SYSTOLIC_MAX = 300
_DIASTOLIC_MIN = 30
_DIASTOLIC_MAX = 200
_CALORIES_BURNED_MIN = 0
_CALORIES_BURNED_MAX = 9999
_MAX_EXERCISE_DESC = 500

_DESC_WEIGHT_KG = f"体重(kg)。{_WEIGHT_MIN}〜{_WEIGHT_MAX}の範囲"
_DESC_STEPS = f"歩数。{_STEPS_MIN}〜{_STEPS_MAX}の整数"
_DESC_BODY_FAT = f"体脂肪率(%)。{_BODY_FAT_MIN}〜{_BODY_FAT_MAX}の範囲"
_DESC_SYSTOLIC = f"収縮期血圧(mmHg)。{_SYSTOLIC_MIN}〜{_SYSTOLIC_MAX}の整数"
_DESC_DIASTOLIC = f"拡張期血圧(mmHg)。{_DIASTOLIC_MIN}〜{_DIASTOLIC_MAX}の整数"
_DESC_CALORIES_BURNED = f"消費カロリー(kcal)。{_CALORIES_BURNED_MIN}〜{_CALORIES_BURNED_MAX}の整数"
_DESC_EXERCISE_DESCRIPTION = f"運動内容。最大{_MAX_EXERCISE_DESC}文字。省略時は MCP"

# 統合ツール（Step8）用の共通文言（P-12）
_DESC_RECORD_TYPE = "meal / weight / steps / body_fat / blood_pressure / exercise / memo のいずれか"
_DESC_RECORD_ID = "list_records で取得したID。memo の場合のみ YYYY-MM-DD 形式の日付を渡す"
_DESC_FIELDS = (
    "更新するフィールドのオブジェクト。許容キー: "
    "weight: weight_kg / steps: steps / body_fat: body_fat_pct / "
    "blood_pressure: systolic, diastolic / exercise: calories_burned, description"
)

# list_records の上限（description 文言とバリデーションを同期させる）
_MAX_LIST_DAYS_MEAL = 31
_MAX_LIST_DAYS_OTHER = 92
_MAX_LIST_RECORDS = 500

# 統合ツール共通のエラーメッセージ（R1: 3ツール間で重複させない）
_MSG_INVALID_RECORD_TYPE = (
    "record_type は meal / weight / steps / body_fat / blood_pressure / exercise / memo のいずれかを指定してください"
)
_MSG_RECORD_NOT_FOUND = "該当レコードが見つかりません"
_MSG_DELETE_RECORD_FETCH_FAILED = "削除前のレコード内容の取得に失敗しました"

_TIME_OF_DAY_JA = {"morning": "朝", "evening": "夜"}


class MealItem(BaseModel):
    """create_meals の items[] の1要素。型検証は SDK に任せ、範囲・業務ルールは record_service で検証する。"""

    description: Annotated[str, Field(description=_DESC_MEAL_FIELDS["description"])]
    calories: Annotated[int | None, Field(description=_DESC_MEAL_FIELDS["calories"])] = None
    protein: Annotated[float | None, Field(description=_DESC_MEAL_FIELDS["protein"])] = None
    fat: Annotated[float | None, Field(description=_DESC_MEAL_FIELDS["fat"])] = None
    carbs: Annotated[float | None, Field(description=_DESC_MEAL_FIELDS["carbs"])] = None
    sodium: Annotated[float | None, Field(description=_DESC_MEAL_FIELDS["sodium"])] = None
    notes: Annotated[str | None, Field(description=_DESC_MEAL_FIELDS["notes"])] = None


def _validate_meal_type(meal_type: str) -> None:
    if meal_type not in record_service.MEAL_TYPES:
        raise record_service.ValidationError(
            "meal_type は breakfast / lunch / dinner / snack / late_night のいずれかを指定してください"
        )


def _validate_meal_fields(**fields) -> None:
    """食事の個別フィールド（品目名・カロリー・PFC・メモ・時刻）を検証する。"""
    if "description" in fields:
        record_service.validate_length("品目名", fields["description"], _MAX_DESC)
    if "calories" in fields:
        record_service.validate_range("カロリー", fields["calories"], 0, _MAX_NUMERIC)
    for key, name in _NUTRIENT_NAMES.items():
        if key in fields:
            record_service.validate_range(name, fields[key], 0, _MAX_NUMERIC)
    if "notes" in fields:
        record_service.validate_length("メモ", fields["notes"], _MAX_NOTES)
    if "meal_time" in fields:
        record_service.validate_time(fields["meal_time"])


def _validate_meal_item(item: MealItem) -> None:
    _validate_meal_fields(**{k: v for k, v in item.model_dump().items() if v is not None})


def _validate_time_of_day(time_of_day: str) -> None:
    if time_of_day not in record_service.TIME_OF_DAY_VALUES:
        raise record_service.ValidationError(
            "time_of_day は morning（朝）または evening（夜）を指定してください"
        )


def _validate_skip_meal_type(meal_type: str) -> None:
    """set_meal_skip の meal_type 検証。database.SKIP_MEAL_TYPES を参照する（R1: 再定義しない）。"""
    if meal_type not in database.SKIP_MEAL_TYPES:
        raise record_service.ValidationError(
            "meal_type は breakfast / lunch / dinner のいずれかを指定してください"
        )


def _json_default(o):
    if isinstance(o, BaseModel):
        return o.model_dump()
    return str(o)


def _safe_tool(fn):
    """P-4: 例外を捕捉して AI に汎用文言のみ返す。ValidationError のみ詳細を返す。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except record_service.ValidationError as exc:
            return {"success": False, "error": exc.message}
        except Exception:
            logger.error("MCPツール実行エラー", exc_info=True)
            return {"success": False, "error": "処理中にエラーが発生しました"}

    return wrapper


_AFFECTED_ID_KEYS = ("meal_ids", "meal_id", "record_id")


def _extract_affected_ids(result: dict):
    """成功戻り値から affected_ids の元になるIDを探す。見つからなければ None。

    create_meals は複数ID（meal_ids）、update_meal 等の単値系は単一ID（meal_id / record_id）。
    """
    for key in _AFFECTED_ID_KEYS:
        if result.get(key) is not None:
            return result[key]
    return None


def _audited(tool_name: str):
    """監査ログを記録するデコレータ。戻り値 dict の success キーで成功/失敗を判定する。

    内側に _safe_tool 相当（例外を {"success": False, ...} dict に変換する）を置く前提。
    監査ログ書き込み自体の失敗はツールを失敗させない（logger.error で記録して続行）。
    """

    def decorator(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                arg_dict = dict(bound.arguments)
            except TypeError:
                arg_dict = dict(kwargs)
            result = await fn(*args, **kwargs)
            if isinstance(result, dict) and result.get("success"):
                affected_ids = _extract_affected_ids(result)
                affected_ids = json.dumps(affected_ids, ensure_ascii=False) if affected_ids is not None else None
                result_kind = "success"
                error_message = None
                # delete_record: 削除前のレコード内容を arguments に追記（誤削除時の追跡 — 設計仕様書 §6.13）
                if result.get("deleted_record") is not None:
                    arg_dict["deleted_record"] = result["deleted_record"]
            else:
                affected_ids = None
                result_kind = "error"
                error_message = result.get("error") if isinstance(result, dict) else None
            arguments = json.dumps(arg_dict, ensure_ascii=False, default=_json_default)
            try:
                database.save_mcp_audit_log(
                    tool_name, arguments, result_kind,
                    affected_ids=affected_ids, error_message=error_message,
                )
            except Exception:
                logger.error("監査ログの書き込みに失敗 (tool=%s)", tool_name, exc_info=True)
            return result

        return wrapper

    return decorator


@_safe_tool
async def get_daily_summary(
    date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
) -> dict:
    """指定日（省略時は論理上の今日）の食事・体重・歩数・体脂肪・スキップ状況と目標値を返す。

    date は原則省略する。ユーザーが明示的に過去日を指定した場合のみ渡すこと。
    目標値（daily_calorie_goal / daily_steps_goal）も併せて返す。
    """
    target_date = date or database.get_logical_today_jst()
    summary = database.get_daily_summary(target_date)
    summary["daily_calorie_goal"] = int(database.get_setting("daily_calorie_goal") or 0)
    summary["daily_steps_goal"] = int(database.get_setting("daily_steps_goal") or 0)
    return {
        "success": True,
        "tool": "get_daily_summary",
        "date": target_date,
        "summary": summary,
    }


@_audited("create_meals")
@_safe_tool
async def create_meals(
    meal_type: Annotated[str, Field(description=_DESC_MEAL_TYPE)],
    items: Annotated[list[MealItem], Field(description=_DESC_ITEMS)],
    date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
    meal_time: Annotated[str | None, Field(description=_DESC_MEAL_TIME)] = None,
) -> dict:
    """食事を複数品目まとめて登録する（写真からの推定登録など）。

    date は原則省略すること。ユーザーが明示的に過去日を指定した場合のみ渡す。
    items の1件でも検証に失敗した場合は全件登録されません。
    """
    target_date = date or database.get_logical_today_jst()
    record_service.validate_date(target_date, allow_future=False)
    _validate_meal_type(meal_type)
    if not items:
        raise record_service.ValidationError("items は1件以上指定してください")
    if meal_time is not None:
        record_service.validate_time(meal_time)
    resolved_time = record_service.resolve_meal_time(target_date, meal_time)
    items = [MealItem.model_validate(i) if isinstance(i, dict) else i for i in items]
    for item in items:
        _validate_meal_item(item)
    item_dicts = [item.model_dump() for item in items]
    meal_ids = database.save_meals_bulk(target_date, meal_type, item_dicts, resolved_time)
    return {
        "success": True,
        "tool": "create_meals",
        "date": target_date,
        "meal_type": meal_type,
        "meal_type_ja": record_service.MEAL_TYPE_JA[meal_type],
        "meal_time": resolved_time,
        "meal_ids": meal_ids,
        "items": item_dicts,
        "total_calories": sum(item.calories or 0 for item in items),
    }


@_audited("update_meal")
@_safe_tool
async def update_meal(
    meal_id: Annotated[int, Field(description=_DESC_MEAL_ID)],
    description: Annotated[str | None, Field(description=_DESC_MEAL_FIELDS["description"])] = None,
    meal_type: Annotated[str | None, Field(description=_DESC_MEAL_TYPE)] = None,
    calories: Annotated[int | None, Field(description=_DESC_MEAL_FIELDS["calories"])] = None,
    protein: Annotated[float | None, Field(description=_DESC_MEAL_FIELDS["protein"])] = None,
    fat: Annotated[float | None, Field(description=_DESC_MEAL_FIELDS["fat"])] = None,
    carbs: Annotated[float | None, Field(description=_DESC_MEAL_FIELDS["carbs"])] = None,
    sodium: Annotated[float | None, Field(description=_DESC_MEAL_FIELDS["sodium"])] = None,
    notes: Annotated[str | None, Field(description=_DESC_MEAL_FIELDS["notes"])] = None,
    meal_time: Annotated[str | None, Field(description=_DESC_MEAL_TIME)] = None,
) -> dict:
    """食事記録を部分更新する。指定されたフィールドのみ更新される。"""
    updates = {k: v for k, v in {
        "description": description,
        "calories": calories,
        "protein": protein,
        "fat": fat,
        "carbs": carbs,
        "sodium": sodium,
        "notes": notes,
        "meal_time": meal_time,
    }.items() if v is not None}
    if meal_type is not None:
        _validate_meal_type(meal_type)
        updates["meal_type"] = meal_type
    if not updates:
        raise record_service.ValidationError("更新するフィールドを1つ以上指定してください")
    _validate_meal_fields(**updates)
    if not database.update_meal(meal_id, **updates):
        raise record_service.ValidationError(_MSG_RECORD_NOT_FOUND)
    return {
        "success": True,
        "tool": "update_meal",
        "meal_id": meal_id,
        "updated_fields": updates,
    }


@_audited("log_weight")
@_safe_tool
async def log_weight(
    weight_kg: Annotated[float, Field(description=_DESC_WEIGHT_KG)],
    time_of_day: Annotated[str, Field(description=_DESC_TIME_OF_DAY)],
    date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
) -> dict:
    """体重を記録する。直前の記録との差（delta）も返す。

    date は原則省略すること。ユーザーが明示的に過去日を指定した場合のみ渡す。
    """
    target_date = date or database.get_logical_today_jst()
    record_service.validate_date(target_date, allow_future=False)
    _validate_time_of_day(time_of_day)
    record_service.validate_range("体重", weight_kg, _WEIGHT_MIN, _WEIGHT_MAX)
    prev = database.get_previous_weight(time_of_day, target_date)
    weight_id = database.save_weight(target_date, time_of_day, weight_kg)
    delta = round(weight_kg - prev, 1) if prev is not None else None
    return {
        "success": True,
        "tool": "log_weight",
        "date": target_date,
        "record_id": weight_id,
        "weight_kg": weight_kg,
        "time_of_day": time_of_day,
        "time_of_day_ja": _TIME_OF_DAY_JA[time_of_day],
        "previous_weight": prev,
        "delta": delta,
    }


@_audited("log_steps")
@_safe_tool
async def log_steps(
    steps: Annotated[int, Field(description=_DESC_STEPS)],
    date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
) -> dict:
    """歩数を記録する（同日は上書き）。既存レコードの上書きかどうかは updated で返す。

    date は原則省略すること。ユーザーが明示的に過去日を指定した場合のみ渡す。
    """
    target_date = date or database.get_logical_today_jst()
    record_service.validate_date(target_date, allow_future=False)
    record_service.validate_range("歩数", steps, _STEPS_MIN, _STEPS_MAX)
    result = database.save_steps(target_date, steps)
    return {
        "success": True,
        "tool": "log_steps",
        "date": target_date,
        "record_id": result["id"],
        "steps": steps,
        "updated": result["updated"],
        "previous_steps": result.get("previous_steps"),
    }


@_audited("log_body_fat")
@_safe_tool
async def log_body_fat(
    body_fat_pct: Annotated[float, Field(description=_DESC_BODY_FAT)],
    date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
) -> dict:
    """体脂肪率(%)を記録する（同日は上書き）。

    date は原則省略すること。ユーザーが明示的に過去日を指定した場合のみ渡す。
    """
    target_date = date or database.get_logical_today_jst()
    record_service.validate_date(target_date, allow_future=False)
    record_service.validate_range("体脂肪率", body_fat_pct, _BODY_FAT_MIN, _BODY_FAT_MAX)
    result = database.save_body_fat(target_date, body_fat_pct)
    return {
        "success": True,
        "tool": "log_body_fat",
        "date": target_date,
        "record_id": result["id"],
        "body_fat_pct": body_fat_pct,
        "updated": result["updated"],
        "previous_body_fat_pct": result.get("previous_body_fat_pct"),
    }


@_audited("log_blood_pressure")
@_safe_tool
async def log_blood_pressure(
    systolic: Annotated[int, Field(description=_DESC_SYSTOLIC)],
    diastolic: Annotated[int, Field(description=_DESC_DIASTOLIC)],
    time_of_day: Annotated[str, Field(description=_DESC_TIME_OF_DAY)],
    date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
) -> dict:
    """血圧を記録する（同日同時間帯は上書き）。

    date は原則省略すること。ユーザーが明示的に過去日を指定した場合のみ渡す。
    """
    target_date = date or database.get_logical_today_jst()
    record_service.validate_date(target_date, allow_future=False)
    _validate_time_of_day(time_of_day)
    record_service.validate_range("収縮期血圧", systolic, _SYSTOLIC_MIN, _SYSTOLIC_MAX)
    record_service.validate_range("拡張期血圧", diastolic, _DIASTOLIC_MIN, _DIASTOLIC_MAX)
    result = database.upsert_blood_pressure(target_date, time_of_day, systolic, diastolic)
    return {
        "success": True,
        "tool": "log_blood_pressure",
        "date": target_date,
        "record_id": result["id"],
        "systolic": systolic,
        "diastolic": diastolic,
        "time_of_day": time_of_day,
        "time_of_day_ja": _TIME_OF_DAY_JA[time_of_day],
        "updated": result["updated"],
    }


@_audited("log_exercise")
@_safe_tool
async def log_exercise(
    calories_burned: Annotated[int, Field(description=_DESC_CALORIES_BURNED)],
    description: Annotated[str, Field(description=_DESC_EXERCISE_DESCRIPTION)] = "MCP",
    date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
) -> dict:
    """運動ログを追加登録する（上書きしない。同日複数回は複数レコードになる）。

    date は原則省略すること。ユーザーが明示的に過去日を指定した場合のみ渡す。
    """
    target_date = date or database.get_logical_today_jst()
    record_service.validate_date(target_date, allow_future=False)
    record_service.validate_range(
        "消費カロリー", calories_burned, _CALORIES_BURNED_MIN, _CALORIES_BURNED_MAX
    )
    description = description or "MCP"
    record_service.validate_length("運動内容", description, _MAX_EXERCISE_DESC)
    exercise_id = database.save_exercise(target_date, calories_burned, description, source="mcp")
    return {
        "success": True,
        "tool": "log_exercise",
        "date": target_date,
        "record_id": exercise_id,
        "calories_burned": calories_burned,
        "description": description,
    }


@_audited("write_memo")
@_safe_tool
async def write_memo(
    text: Annotated[str, Field(description=_DESC_MEMO_TEXT)],
    date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
    mode: Annotated[str, Field(description=_DESC_MODE)] = "replace",
) -> dict:
    """当日のメモを書き込む。mode="replace" は全文置換、mode="append" は末尾追記。

    date は原則省略すること。ユーザーが明示的に過去日を指定した場合のみ渡す。
    """
    target_date = date or database.get_logical_today_jst()
    record_service.validate_date(target_date, allow_future=False)
    sanitized = record_service.sanitize_memo_text(text, mode)
    if mode == "replace":
        result = database.upsert_memo(target_date, sanitized)
        current_total_chars = len(sanitized)
    else:
        result = database.append_memo(target_date, sanitized)
        current_total_chars = result["current_total_chars"]
    return {
        "success": True,
        "tool": "write_memo",
        "date": target_date,
        "mode": mode,
        "status": result["status"],
        "current_total_chars": current_total_chars,
    }


@_audited("set_meal_skip")
@_safe_tool
async def set_meal_skip(
    meal_type: Annotated[str, Field(description=_DESC_SKIP_MEAL_TYPE)],
    skipped: Annotated[bool, Field(description=_DESC_SKIPPED)],
    date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
) -> dict:
    """食事のスキップを設定/解除する。skipped=True で設定、False で解除。

    date は原則省略すること。ユーザーが明示的に過去日を指定した場合のみ渡す。
    """
    target_date = date or database.get_logical_today_jst()
    record_service.validate_date(target_date, allow_future=False)
    _validate_skip_meal_type(meal_type)
    if skipped:
        database.save_meal_skip(target_date, meal_type)
        deleted = None
    else:
        deleted = database.delete_meal_skip(target_date, meal_type)
    return {
        "success": True,
        "tool": "set_meal_skip",
        "date": target_date,
        "meal_type": meal_type,
        "meal_type_ja": record_service.MEAL_TYPE_JA[meal_type],
        "skipped": skipped,
        "deleted": deleted,
    }


# ── 統合ツール（Step8）─ ディスパッチテーブル（CLAUDE.md §4: if-elif チェーン禁止）──

_RECORD_TYPES: dict[str, dict] = {
    "meal": {
        "label": "食事",
        "delete": (record_service, "delete_meal_with_images"),  # P-10: 画像ファイル実体も削除
        "read": (database, "get_meal_by_id"),
    },
    "weight": {
        "label": "体重",
        "module": database,
        "update": "update_weight_by_id",
        "delete": "delete_weight_by_id",
        "read": "get_weight_by_id",
        "fields": ("weight_kg",),
    },
    "steps": {
        "label": "歩数",
        "module": database,
        "update": "update_steps_by_id",
        "delete": "delete_steps_by_id",
        "read": "get_steps_by_id",
        "fields": ("steps",),
    },
    "body_fat": {
        "label": "体脂肪率",
        "module": database,
        "update": "update_body_fat_by_id",
        "delete": "delete_body_fat_by_id",
        "read": "get_body_fat_by_id",
        "fields": ("body_fat_pct",),
    },
    "blood_pressure": {
        "label": "血圧",
        "module": database,
        "update": "update_blood_pressure_by_id",
        "delete": "delete_blood_pressure_by_id",
        "fields": ("systolic", "diastolic"),
        "read": "get_blood_pressure_by_id",  # C-5: 部分更新のマージ元
    },
    "exercise": {
        "label": "運動",
        "module": database,
        "update": "update_exercise_by_id",
        "delete": "delete_exercise_by_id",
        "fields": ("calories_burned", "description"),
        "read": "get_exercise_by_id",        # C-5: 部分更新のマージ元
    },
    "memo": {
        "label": "メモ",
        "module": database,
        "delete": "delete_memo",
        "read": "get_memo",
    },
}


def _resolve_record_fn(info: dict, key: str):
    """ディスパッチテーブルの関数を呼び出し時点でモジュール属性から解決する。

    値は (モジュール, 関数名) のタプルまたは {key}_module のモジュール + 関数名文字列。
    インポート時に関数オブジェクトを掴むと monkeypatch 等の差し替えが効かず、
    内部例外を再現できないため、呼び出し時に getattr で解決する。
    """
    value = info[key]
    if isinstance(value, tuple):
        module, func_name = value
    else:
        module = info.get(key + "_module", info["module"])
        func_name = value
    return getattr(module, func_name)


_UPDATE_FIELD_VALIDATORS = {
    "weight_kg": lambda v: record_service.validate_range("体重", v, _WEIGHT_MIN, _WEIGHT_MAX),
    "steps": lambda v: record_service.validate_range("歩数", v, _STEPS_MIN, _STEPS_MAX),
    "body_fat_pct": lambda v: record_service.validate_range("体脂肪率", v, _BODY_FAT_MIN, _BODY_FAT_MAX),
    "systolic": lambda v: record_service.validate_range("収縮期血圧", v, _SYSTOLIC_MIN, _SYSTOLIC_MAX),
    "diastolic": lambda v: record_service.validate_range("拡張期血圧", v, _DIASTOLIC_MIN, _DIASTOLIC_MAX),
    "calories_burned": lambda v: record_service.validate_range(
        "消費カロリー", v, _CALORIES_BURNED_MIN, _CALORIES_BURNED_MAX
    ),
    "description": lambda v: record_service.validate_length("運動内容", v, _MAX_EXERCISE_DESC),
}


def _flat_meal(day: dict) -> list[dict]:
    return day["meals"]


def _flat_weight(day: dict) -> list[dict]:
    return [
        {"log_date": day["date"], "time_of_day": tod, **rec}
        for tod, rec in day["weight"].items()
    ]


def _flat_steps(day: dict) -> list[dict]:
    if day["steps_id"] is None:
        return []
    return [{"log_date": day["date"], "steps": day["steps"], "id": day["steps_id"]}]


def _flat_body_fat(day: dict) -> list[dict]:
    if day["body_fat_id"] is None:
        return []
    return [{"log_date": day["date"], "body_fat_pct": day["body_fat"], "id": day["body_fat_id"]}]


def _flat_blood_pressure(day: dict) -> list[dict]:
    return [
        {"log_date": day["date"], "time_of_day": tod, **rec}
        for tod, rec in day["blood_pressure"].items()
    ]


def _flat_exercise(day: dict) -> list[dict]:
    return day["exercise"]


# get_history の日付ネスト構造から種別ごとにフラット化する（id を含む）
_HISTORY_EXTRACTORS = {
    "meal": _flat_meal,
    "weight": _flat_weight,
    "steps": _flat_steps,
    "body_fat": _flat_body_fat,
    "blood_pressure": _flat_blood_pressure,
    "exercise": _flat_exercise,
}


def _flatten_history(record_type: str, history: list[dict]) -> list[dict]:
    extract = _HISTORY_EXTRACTORS[record_type]
    return [rec for day in history for rec in extract(day)]


def _fetch_deleted_record(record_type: str, record_id) -> dict:
    """削除前のレコード内容を取得する。取得できない場合はその旨の dict を返す。

    取得に失敗しても削除自体は実行する（呼び出し元が継続する — 設計仕様書 §6.13）。
    """
    info = _RECORD_TYPES.get(record_type, {})
    if "read" not in info:
        return {"note": _MSG_DELETE_RECORD_FETCH_FAILED}
    reader = _resolve_record_fn(info, "read")
    try:
        content = reader(record_id)
    except Exception:
        logger.warning(
            "delete_record: 削除前内容の取得に失敗 (record_type=%s)", record_type, exc_info=True
        )
        content = None
    if content is None:
        return {"note": _MSG_DELETE_RECORD_FETCH_FAILED}
    return content


@_safe_tool
async def list_records(
    record_type: Annotated[str, Field(description=_DESC_RECORD_TYPE)],
    start_date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
    end_date: Annotated[str | None, Field(description=_DESC_DATE)] = None,
) -> dict:
    """記録一覧を取得する（読み取り専用のため監査ログは記録されない）。

    start_date / end_date は原則省略する。ユーザーが明示的に過去日を指定した場合のみ渡す。
    食事は最大31日・食事以外は最大92日。500件を超える場合は期間を狭めること。
    """
    if record_type not in _RECORD_TYPES:
        raise record_service.ValidationError(
            _MSG_INVALID_RECORD_TYPE
        )
    start = start_date or database.get_logical_today_jst()
    end = end_date or start
    record_service.validate_date(start, allow_future=True)
    record_service.validate_date(end, allow_future=True)
    max_days = _MAX_LIST_DAYS_MEAL if record_type == "meal" else _MAX_LIST_DAYS_OTHER
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if span < 1:
        raise record_service.ValidationError("start_date は end_date 以前の日付を指定してください")
    if span > max_days:
        raise record_service.ValidationError(
            f"{record_type} は一度に {max_days} 日までしか指定できません。期間を狭めてください"
        )
    if record_type == "memo":
        records = database.get_memos_range(start, end)
    else:
        records = _flatten_history(record_type, database.get_history(start_date=start, end_date=end))
    if len(records) > _MAX_LIST_RECORDS:
        raise record_service.ValidationError(
            f"件数が{_MAX_LIST_RECORDS}件を超えています。期間を狭めてください"
        )
    return {
        "success": True,
        "tool": "list_records",
        "record_type": record_type,
        "start_date": start,
        "end_date": end,
        "records": records,
    }


@_audited("update_record")
@_safe_tool
async def update_record(
    record_type: Annotated[str, Field(description=_DESC_RECORD_TYPE)],
    record_id: Annotated[int, Field(description=_DESC_RECORD_ID)],
    fields: Annotated[dict, Field(description=_DESC_FIELDS)],
) -> dict:
    """記録を更新する（食事は対象外。update_meal を使うこと）。

    record_type ごとに許容されるキー以外を指定するとエラーになる。
    blood_pressure / exercise は一部のキーのみ指定しても、既存値を保持したまま部分更新できる。
    """
    info = _RECORD_TYPES.get(record_type)
    if info is None:
        raise record_service.ValidationError(
            _MSG_INVALID_RECORD_TYPE
        )
    if record_type == "meal":
        raise record_service.ValidationError("食事の更新は update_record ではなく update_meal を使ってください")
    update_fn = _resolve_record_fn(info, "update") if "update" in info else None
    if update_fn is None:
        raise record_service.ValidationError(
            f"{info['label']} の更新は update_record の対象外です。write_memo を使ってください"
        )
    allowed = set(info["fields"])
    extra = set(fields) - allowed
    if extra:
        raise record_service.ValidationError(
            f"更新できないキーが含まれています: {', '.join(sorted(extra))}"
        )
    if not fields:
        raise record_service.ValidationError("fields に更新するキーを1つ以上指定してください")
    for key, value in fields.items():
        _UPDATE_FIELD_VALIDATORS[key](value)
    values = dict(fields)
    # C-5: blood_pressure / exercise は既存 update 関数が全フィールド必須のため、既存値を読み出してマージする
    if "read" in info:
        existing = _resolve_record_fn(info, "read")(record_id)
        if existing is None:
            raise record_service.ValidationError(_MSG_RECORD_NOT_FOUND)
        for key in allowed:
            if key not in values:
                values[key] = existing[key]
    if not update_fn(record_id, **values):
        raise record_service.ValidationError(_MSG_RECORD_NOT_FOUND)
    return {
        "success": True,
        "tool": "update_record",
        "record_type": record_type,
        "record_id": record_id,
        "updated_fields": fields,
    }


@_audited("delete_record")
@_safe_tool
async def delete_record(
    record_type: Annotated[str, Field(description=_DESC_RECORD_TYPE)],
    record_id: Annotated[int | str, Field(description=_DESC_RECORD_ID)],
) -> dict:
    """記録を1件削除する（一括削除・日付範囲削除は提供しない）。

    memo は record_id の代わりに YYYY-MM-DD 形式の日付を渡す。
    削除前にレコード内容を取得し、監査ログの arguments に残す。
    """
    info = _RECORD_TYPES.get(record_type)
    if info is None:
        raise record_service.ValidationError(
            _MSG_INVALID_RECORD_TYPE
        )
    if record_type == "memo":
        log_date = str(record_id)
        record_service.validate_date(log_date, allow_future=True)
        deleted_record = _fetch_deleted_record("memo", log_date)
        if not database.delete_memo(log_date):
            raise record_service.ValidationError(_MSG_RECORD_NOT_FOUND)
        return {
            "success": True,
            "tool": "delete_record",
            "record_type": "memo",
            "record_id": log_date,
            "deleted_record": deleted_record,
        }
    delete_fn = _resolve_record_fn(info, "delete")
    deleted_record = _fetch_deleted_record(record_type, record_id)
    if not delete_fn(record_id):
        raise record_service.ValidationError(_MSG_RECORD_NOT_FOUND)
    return {
        "success": True,
        "tool": "delete_record",
        "record_type": record_type,
        "record_id": record_id,
        "deleted_record": deleted_record,
    }


def build_mcp_server(instructions: str | None = None) -> MCPServer:
    """MCPServer を構築し、ツールを登録して返す。"""
    server = MCPServer(name="healthreport", instructions=instructions)
    server.tool()(get_daily_summary)
    server.tool()(create_meals)
    server.tool()(update_meal)
    server.tool()(log_weight)
    server.tool()(log_steps)
    server.tool()(log_body_fat)
    server.tool()(log_blood_pressure)
    server.tool()(log_exercise)
    server.tool()(write_memo)
    server.tool()(set_meal_skip)
    server.tool()(list_records)
    server.tool()(update_record)
    server.tool()(delete_record)
    return server


def build_mcp_asgi(server: MCPServer):
    """MCPServer を Streamable HTTP の Starlette アプリとして返す。"""
    return server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            # P-2: DNSリバインディング保護は既定 ON で Host ヘッダを検証する。
            # nginx 経由の公開ドメインからアクセスするため、ここで無効化する。
            enable_dns_rebinding_protection=False,
        ),
    )

"""MCP サーバー定義。

claude.ai（Web版）カスタムコネクタから HealthReport の記録操作を提供する。
推論はクライアント側 AI が担い、本サーバーは Anthropic API を呼ばない（設計仕様書 §1.4）。
"""
import functools
import inspect
import json
import logging
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
                arguments = json.dumps(bound.arguments, ensure_ascii=False, default=_json_default)
            except TypeError:
                arguments = json.dumps(kwargs, ensure_ascii=False, default=_json_default)
            result = await fn(*args, **kwargs)
            if isinstance(result, dict) and result.get("success"):
                affected_ids = _extract_affected_ids(result)
                affected_ids = json.dumps(affected_ids, ensure_ascii=False) if affected_ids is not None else None
                result_kind = "success"
                error_message = None
            else:
                affected_ids = None
                result_kind = "error"
                error_message = result.get("error") if isinstance(result, dict) else None
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


async def get_daily_summary(date: str | None = None) -> dict:
    """指定日（省略時は論理上の今日）の食事・体重・歩数・体脂肪・スキップ状況を返す。

    date は原則省略する。ユーザーが明示的に過去日を指定した場合のみ渡すこと。
    """
    target_date = date or database.get_logical_today_jst()
    return {
        "success": True,
        "tool": "get_daily_summary",
        "date": target_date,
        "summary": database.get_daily_summary(target_date),
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
        raise record_service.ValidationError("該当レコードが見つかりません")
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

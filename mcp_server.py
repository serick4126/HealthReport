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


def build_mcp_server(instructions: str | None = None) -> MCPServer:
    """MCPServer を構築し、ツールを登録して返す。"""
    server = MCPServer(name="healthreport", instructions=instructions)
    server.tool()(get_daily_summary)
    server.tool()(create_meals)
    server.tool()(update_meal)
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

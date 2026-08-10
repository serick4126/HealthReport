"""
record_service.py — FastAPI/MCP に依存しない共通ドメインロジック

mcp_server.py と main.py の重複（R1違反）を防ぐため、バリデーション・
時刻解決・定数・画像削除を共通化する。
HTTPException は送出せず、独自の ValidationError を送出する。

文言の層間契約:
- ValidationError.message は MCP 層（mcp_server.py）がそのまま
  {"success": False, "error": message} として利用する正である。
- HTTP 層（main.py）は既存 HTTP 挙動不変（P-6）のため、既存 detail 文言が
  record_service の生成文言（MSG_* 定数）と一致する経路では str(exc) を
  そのまま利用し、一致しない経路のみ既存 detail 文言で上書き変換する。
"""

import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import database

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# 既存HTTP detail 文言と同一のメッセージ定数（P-6: 文言不変）
MSG_DATE_FORMAT = "dateはYYYY-MM-DD形式で指定してください"
MSG_DATE_FUTURE = "未来の日付は登録できません"
MSG_TIME_FORMAT = "timeはHH:MM形式で指定してください"
MSG_TIME_RANGE = "timeの時・分が範囲外です（時: 0-23, 分: 0-59）"
MSG_MEMO_EMPTY = "メモを入力してください"
MSG_MEMO_REPLACE_TOO_LONG = "memo_text が2000字を超えています"
MSG_MEMO_APPEND_TOO_LONG = "1回の追記は500字以内にしてください"

# ValidationError.code 定数（層間で比較するためマジック文字列を回避）
CODE_VALIDATION_ERROR = "validation_error"
CODE_INVALID_DATE_FORMAT = "invalid_date_format"
CODE_FUTURE_DATE = "future_date"
CODE_OUT_OF_RANGE = "out_of_range"
CODE_TOO_LONG = "too_long"
CODE_INVALID_TIME_FORMAT = "invalid_time_format"
CODE_INVALID_TIME_RANGE = "invalid_time_range"
CODE_INVALID_MODE = "invalid_mode"
CODE_EMPTY_MEMO = "empty_memo"
CODE_MEMO_REPLACE_TOO_LONG = "memo_replace_too_long"
CODE_MEMO_APPEND_TOO_LONG = "memo_append_too_long"

MEAL_TYPE_JA = {
    "breakfast": "朝食",
    "lunch": "昼食",
    "dinner": "夕食",
    "snack": "間食",
    "late_night": "夜食",
}

MEAL_TYPES = ["breakfast", "lunch", "dinner", "snack", "late_night"]
TIME_OF_DAY_VALUES = ["morning", "evening"]

_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\t]")


class ValidationError(Exception):
    """入力検証エラー。HTTP層は捕捉して HTTPException に変換する。
    code は検証種別を識別するための文字列（main.py 側の文言マッピング用）。
    """

    def __init__(self, message: str, code: str = CODE_VALIDATION_ERROR):
        super().__init__(message)
        self.message = message
        self.code = code


def validate_date(date_str: str, *, allow_future: bool = False) -> None:
    """YYYY-MM-DD 形式の日付を検証。不正な場合は ValidationError を送出。
    allow_future=False（既定）のとき未来日付も拒否する。
    ゼロパディング欠落の許容など strptime の挙動は既存実装と同一（P-6）。
    """
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise ValidationError(MSG_DATE_FORMAT, code=CODE_INVALID_DATE_FORMAT)
    if not allow_future and parsed > datetime.now(JST).date():
        raise ValidationError(MSG_DATE_FUTURE, code=CODE_FUTURE_DATE)


def validate_time(time_str: str) -> None:
    """HH:MM 形式の時刻を検証。不正な場合は ValidationError を送出。"""
    if not _TIME_RE.match(time_str):
        raise ValidationError(MSG_TIME_FORMAT, code=CODE_INVALID_TIME_FORMAT)
    h, m = int(time_str[:2]), int(time_str[3:])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValidationError(MSG_TIME_RANGE, code=CODE_INVALID_TIME_RANGE)


def validate_range(name: str, value: float, lo: float, hi: float) -> None:
    """数値が [lo, hi] の範囲内かを検証。範囲外は ValidationError を送出。"""
    if value < lo or value > hi:
        raise ValidationError(
            f"{name} は {lo}〜{hi} の範囲で指定してください", code=CODE_OUT_OF_RANGE
        )


def validate_length(name: str, text: str, max_len: int) -> None:
    """文字列長が max_len を超えていないかを検証。超過時は ValidationError を送出。"""
    if len(text) > max_len:
        raise ValidationError(
            f"{name} は {max_len} 文字以内で指定してください", code=CODE_TOO_LONG
        )


def sanitize_memo_text(text: str, mode: str) -> str:
    """メモテキストをサニタイズ・検証。制御文字を除去し長さをチェックする。
    mode="replace" は2000字、mode="append" は500字上限。
    空文字・超過時は ValidationError を送出し、サニタイズ済み文字列を返す。
    """
    if mode not in ("replace", "append"):
        raise ValidationError(
            f"mode は replace / append を指定してください: {mode}", code=CODE_INVALID_MODE
        )
    sanitized = _CTRL_RE.sub("", text)
    if sanitized.strip() == "":
        raise ValidationError(MSG_MEMO_EMPTY, code=CODE_EMPTY_MEMO)
    if mode == "replace" and len(sanitized) > 2000:
        raise ValidationError(MSG_MEMO_REPLACE_TOO_LONG, code=CODE_MEMO_REPLACE_TOO_LONG)
    if mode == "append" and len(sanitized) > 500:
        raise ValidationError(MSG_MEMO_APPEND_TOO_LONG, code=CODE_MEMO_APPEND_TOO_LONG)
    return sanitized


def resolve_meal_time(meal_date: Optional[str], explicit: Optional[str]) -> Optional[str]:
    """食事時刻の解決。
    - explicit が指定されていればそのまま返す
    - meal_date 未指定 or 当日で explicit なし → 現在時刻 HH:MM
    - 過去日で explicit なし → None（未設定のまま）
    """
    if explicit is not None:
        return explicit
    today_str = database.get_logical_today_jst()
    target = meal_date if meal_date is not None else today_str
    if target == today_str:
        return datetime.now(JST).strftime("%H:%M")
    return None


def delete_meal_with_images(meal_id: int) -> bool:
    """食事記録と紐づく画像ファイル実体をまとめて削除する。
    レコード削除に失敗したら False を返す。
    画像ファイルの unlink 失敗は警告ログのみで例外を投げず True を返す。
    """
    images = database.get_meal_images(meal_id)
    ok = database.delete_meal(meal_id)
    if not ok:
        return False
    for img in images:
        if img.get("image_path"):
            try:
                Path(img["image_path"]).unlink(missing_ok=True)
            except Exception:
                logger.warning("画像ファイルの削除に失敗 (path=%s)", img["image_path"], exc_info=True)
    return True

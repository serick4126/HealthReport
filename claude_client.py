import asyncio
import base64
import inspect
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

import anthropic

import database
import food_search
from image_utils import process_image_b64, save_image_to_fs

JST = timezone(timedelta(hours=9))
MODEL = "claude-sonnet-4-6"
MODEL_SAVINGS = "claude-haiku-4-5-20251001"
MAX_API_RETRIES = 3  # 429エラー時の最大リトライ回数


def _format_error_message(e: Exception) -> str:
    """例外を日本語のユーザー向けメッセージに変換する"""
    if isinstance(e, anthropic.RateLimitError):
        return "APIのレート制限が続いています。数分後に再度お試しください。"
    elif isinstance(e, anthropic.AuthenticationError):
        return "APIキーが無効です。設定画面でAPIキーを確認してください。"
    elif isinstance(e, anthropic.APIConnectionError):
        return "ネットワーク接続エラーが発生しました。インターネット接続を確認してください。"
    elif isinstance(e, anthropic.BadRequestError):
        return "リクエストが不正です。会話履歴をリセットしてから再試行してください。"
    elif isinstance(e, anthropic.APIStatusError):
        return f"APIエラーが発生しました（コード: {e.status_code}）。しばらく待ってから再試行してください。"
    elif isinstance(e, ValueError) and "ANTHROPIC_API_KEY" in str(e):
        return "APIキーが設定されていません。設定画面でAPIキーを入力してください。"
    else:
        return f"予期しないエラーが発生しました。会話履歴をリセットしてから再試行してください。"

# ── ツール定義 ─────────────────────────────────────────────────────────────────

TOOLS: list[anthropic.types.ToolParam] = [
    {
        "name": "record_meal",
        "description": "食事記録をデータベースに保存します。カロリー・PFC・塩分は必ず入力してください。",
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_date": {"type": "string", "description": "食事の日付 (YYYY-MM-DD形式)"},
                "meal_type": {
                    "type": "string",
                    "enum": ["breakfast", "lunch", "dinner", "snack", "late_night"],
                    "description": "食事区分: breakfast=朝食, lunch=昼食, dinner=夕食, snack=間食, late_night=夜食",
                },
                "description": {"type": "string", "description": "食事内容の説明"},
                "calories": {"type": "integer", "description": "カロリー (kcal)"},
                "protein": {"type": "number", "description": "タンパク質 (g)"},
                "fat": {"type": "number", "description": "脂質 (g)"},
                "carbs": {"type": "number", "description": "炭水化物・糖質 (g)"},
                "sodium": {"type": "number", "description": "塩分相当量 (g)"},
                "notes": {"type": "string", "description": "備考。推定値の場合は '[推定値] カロリー・PFCはClaudeによる推定' を含める。Slismで検索した場合は '[Slism]' を含める。"},
                "image_source_type": {
                    "type": "string",
                    "enum": ["photo", "label", "barcode"],
                    "description": "画像から記録する場合の画像種別。photo=料理写真, label=栄養成分ラベル, barcode=バーコード",
                },
                "meal_time": {
                    "type": "string",
                    "description": (
                        "実際に食べた時刻（HH:MM形式）。"
                        "会話中に「7時に食べた」「昼の12時ごろ」など時刻が明示されている場合は"
                        "その時刻を「HH:00」形式で設定する（例: 「7時」→「07:00」、「12時半」→「12:00」）。"
                        "時刻が明示されていない場合は省略すること（省略時は当日なら送信時刻を自動設定）。"
                    ),
                },
            },
            "required": ["meal_date", "meal_type", "description", "calories", "protein", "fat", "carbs", "sodium"],
        },
    },
    {
        "name": "record_weight",
        "description": "体重記録をデータベースに保存します",
        "input_schema": {
            "type": "object",
            "properties": {
                "log_date": {"type": "string", "description": "記録日 (YYYY-MM-DD形式)"},
                "time_of_day": {
                    "type": "string",
                    "enum": ["morning", "evening"],
                    "description": "測定時間帯: morning=朝, evening=夜",
                },
                "weight_kg": {"type": "number", "description": "体重 (kg)"},
            },
            "required": ["log_date", "time_of_day", "weight_kg"],
        },
    },
    {
        "name": "record_steps",
        "description": "歩数記録をデータベースに保存します。同日の再記録は自動的に上書きされます。",
        "input_schema": {
            "type": "object",
            "properties": {
                "log_date": {"type": "string", "description": "記録日 (YYYY-MM-DD形式)"},
                "steps": {"type": "integer", "description": "歩数"},
            },
            "required": ["log_date", "steps"],
        },
    },
    {
        "name": "get_daily_summary",
        "description": "指定日の食事・体重・歩数の記録サマリーを取得します。食事提案や残りカロリー計算に使用してください。",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_date": {"type": "string", "description": "対象日 (YYYY-MM-DD形式)。省略時は今日。"},
            },
        },
    },
    {
        "name": "update_meal",
        "description": "既存の食事記録を更新します。修正依頼時に使用してください。",
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_id": {"type": "integer", "description": "更新するレコードのID"},
                "description": {"type": "string"},
                "meal_type": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack", "late_night"]},
                "calories": {"type": "integer"},
                "protein": {"type": "number"},
                "fat": {"type": "number"},
                "carbs": {"type": "number"},
                "sodium": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": ["meal_id"],
        },
    },
    # ── Phase 2 追加ツール ──────────────────────────────────────────────────────
    {
        "name": "search_food_nutrition",
        "description": (
            "カロリーSlismで食品の栄養情報を検索します。"
            "ブランド品・パッケージ食品・飲料など、正確なカロリー・PFCが必要な場合に使用してください。"
            "一般的な自炊食材（白米・鶏むね肉など）はClaudeの知識で推定してください。"
            "検索結果が複数ある場合はshow_choicesで選択肢を表示してください。"
            "1件のみの場合はそのまま採用してrecord_mealを呼んでください。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "food_name": {"type": "string", "description": "検索する食品名（例：ヤクルト1000、クノールカップスープ）"},
                "amount": {"type": "string", "description": "量（例：1本、65ml、100g）。省略可。"},
            },
            "required": ["food_name"],
        },
    },
    {
        "name": "save_food_default",
        "description": (
            "食品のデフォルト設定をDBに保存します。"
            "推定値またはSlism検索で記録した食品を次回から自動適用するために、record_meal成功後に呼んでください。"
            "food_defaultsに同じキーワードが既に存在する場合は上書きします。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "食品名キーワード（例：ヤクルト1000）"},
                "description": {"type": "string", "description": "栄養情報の説明（例：ヤクルト1000 Light 1本65ml 47kcal P:1.5g F:0g C:10.1g 塩:0.04g）"},
                "notes": {"type": "string", "description": "備考（任意）"},
            },
            "required": ["keyword", "description"],
        },
    },
    {
        "name": "show_choices",
        "description": (
            "ユーザーに選択肢ボタンを表示します。"
            "食品バリアント（サイズ・量違い等）の確認、または「該当なし」フローに使用してください。"
            "show_choicesを呼び出した後は追加のテキストを出力しないこと。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "ユーザーへの質問文"},
                "options": {
                    "type": "array",
                    "description": "選択肢のリスト。食品検索の場合は最後に {'label': '🔍 該当する商品がない', 'value': '該当なし'} を必ず追加すること。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "ボタンに表示するテキスト（例：ヤクルト1000（65ml）47kcal）"},
                            "value": {"type": "string", "description": "選択時にチャットへ送信するテキスト"},
                        },
                        "required": ["label", "value"],
                    },
                },
            },
            "required": ["question", "options"],
        },
    },
    # ── Phase 11 追加ツール ─────────────────────────────────────────────────────
    # ※ enum の値は database.SKIP_MEAL_TYPES と同期すること
    {
        "name": "record_meal_skip",
        "description": (
            "朝食・昼食・夕食を意図的に食べなかった場合に記録します。"
            "「朝食抜いた」「昼ごはん食べなかった」「夕食スキップ」などの報告時に使用してください。"
            "間食・夜食は対象外です。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_date": {"type": "string", "description": "食事の日付 (YYYY-MM-DD形式)"},
                "meal_type": {
                    "type": "string",
                    "enum": ["breakfast", "lunch", "dinner"],
                    "description": "食事区分: breakfast=朝食, lunch=昼食, dinner=夕食",
                },
            },
            "required": ["meal_date", "meal_type"],
        },
    },
    {
        "name": "delete_meal_skip",
        "description": (
            "記録済みの食事スキップを取り消します。"
            "「やっぱり朝食食べた」「スキップ記録を消して」などの訂正時に使用してください。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_date": {"type": "string", "description": "食事の日付 (YYYY-MM-DD形式)"},
                "meal_type": {
                    "type": "string",
                    "enum": ["breakfast", "lunch", "dinner"],
                    "description": "食事区分: breakfast=朝食, lunch=昼食, dinner=夕食",
                },
            },
            "required": ["meal_date", "meal_type"],
        },
    },
    # ── Phase 12 追加ツール ─────────────────────────────────────────────────────
    {
        "name": "record_sleep",
        "description": "睡眠ログを記録する。Apple Watch等から取得した就寝・起床時刻と睡眠ステージを保存。",
        "input_schema": {
            "type": "object",
            "properties": {
                "date":          {"type": "string",  "description": "就寝日 YYYY-MM-DD"},
                "sleep_start":   {"type": "string",  "description": "就寝時刻 HH:MM"},
                "sleep_end":     {"type": "string",  "description": "起床時刻 HH:MM"},
                "deep_minutes":  {"type": "integer", "description": "深睡眠（分）"},
                "rem_minutes":   {"type": "integer", "description": "REM睡眠（分）"},
                "awake_minutes": {"type": "integer", "description": "覚醒（分）"},
            },
            "required": ["date", "sleep_start", "sleep_end"],
        },
    },
    {
        "name": "get_sleep_summary",
        "description": "指定期間の睡眠サマリーを取得する（睡眠時間・ステージ等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "開始日 YYYY-MM-DD"},
                "end_date":   {"type": "string", "description": "終了日 YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "record_vital",
        "description": (
            "バイタルデータを記録する。"
            "type='heart_rate'/'spo2' の場合は value が必須（心拍数bpm / SpO2%）。"
            "type='bp_alert' の場合は value 不要（note で通知内容を記録可能）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date":  {"type": "string",  "description": "記録日 YYYY-MM-DD"},
                "type":  {"type": "string",  "enum": ["heart_rate", "spo2", "bp_alert"], "description": "バイタル種別"},
                "value": {"type": "number",  "description": "脈拍(bpm)またはSpO2(%)。bp_alertは不要"},
                "time":  {"type": "string",  "description": "測定時刻 HH:MM（省略可）"},
                "note":  {"type": "string",  "description": "高血圧通知時のメモ（省略可）"},
            },
            "required": ["date", "type"],
        },
    },
    {
        "name": "get_vital_summary",
        "description": "指定期間のバイタルサマリーを取得する（脈拍・SpO2等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "開始日 YYYY-MM-DD"},
                "end_date":   {"type": "string", "description": "終了日 YYYY-MM-DD"},
                "type":       {"type": "string", "enum": ["heart_rate", "spo2", "bp_alert"], "description": "絞り込むバイタル種別（省略で全種別）"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_bmi_info",
        "description": "最新体重と身長設定からBMIと推定基礎代謝量を計算して返す。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "record_exercise",
        "description": (
            "ユーザーが運動した内容を記録する。運動内容から消費カロリーを推定し、DBに保存する。"
            "ユーザーが消費カロリーを明示した場合はその値をそのまま使用する。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "log_date": {
                    "type": "string",
                    "description": "記録日（YYYY-MM-DD）。ユーザーが指定しない場合は今日の日付。",
                },
                "calories_burned": {
                    "type": "integer",
                    "description": (
                        "推定消費カロリー（kcal）。"
                        "運動の種類・強度・時間・体重から推定する。0〜9999 の整数。"
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "運動内容の説明（例：「30分ジョギング 5km」「筋トレ45分」）。500文字以内。",
                },
            },
            "required": ["log_date", "calories_burned", "description"],
        },
    },
]


# ── food_defaults スマートマッチング ───────────────────────────────────────────

def _match_food_defaults(user_message: str, food_defaults: list[dict]) -> list[dict]:
    """
    ユーザーメッセージに含まれるキーワードに一致するfood_defaultsエントリを返す。
    非AI・部分文字列マッチングのみ使用（トークン節約）。

    重複除外: 他のマッチ済みキーワードのサブストリングになっている短いキーワードは除外する。
    例: 「ヤクルト1000」にマッチした場合、「ヤクルト100」（サブストリング）は除外。
    """
    if not food_defaults:
        return []
    msg = user_message.lower()
    matched = [fd for fd in food_defaults if fd["keyword"].lower() in msg]

    # より長いマッチ済みキーワードに包含されている短いキーワードを除外
    matched_kws = {fd["keyword"].lower() for fd in matched}
    return [
        fd for fd in matched
        if not any(
            fd["keyword"].lower() != other and fd["keyword"].lower() in other
            for other in matched_kws
        )
    ]


# ── システムプロンプト ──────────────────────────────────────────────────────────

def _build_search_flow_section(savings_mode: bool) -> str:
    """食品検索フローセクションを返す"""
    if savings_mode:
        return (
            "（節約モード）すべての食品はClaudeの知識で直接推定してrecord_mealを呼ぶこと。"
            "search_food_nutritionは使用しない。"
            "notesに '[推定値] カロリー・PFCはClaudeによる推定' を記載すること。"
        )
    return (
        "ブランド品・パッケージ食品・飲料が報告された場合：\n"
        "1. search_food_nutrition で検索する\n"
        "2. 複数候補 → show_choices で表示（必ず最後に「\U0001f50d 該当する商品がない」を追加）\n"
        "3. 1候補のみ → そのまま採用して record_meal を呼ぶ\n"
        "4. 0候補（見つからない）→ Claudeの知識で推定して自動的に record_meal を呼ぶ。ユーザーに確認しない。\n"
        "   notesに '[推定値] カロリー・PFCはClaudeによる推定' を記載すること。\n"
        "鶏むね肉・白米・卵などの一般食材はClaudeの知識で推定してよい。\n"
        "show_choicesを呼び出した後は余分なテキストを出力しないこと。"
    )


def _build_auto_save_section(enabled: bool) -> str:
    """food_defaults自動保存セクションを返す"""
    if not enabled:
        return ""
    return (
        "\n【food_defaults自動保存】\n"
        "推定値([推定値])またはSlism検索([Slism])でrecord_mealが成功した場合、\n"
        "food_defaultsに同じキーワードが未登録であればsave_food_defaultを呼んで自動保存すること。\n"
        "保存するキーワードは食品の主要名（例：「ヤクルト1000」「コーヒー牛乳」）とし、\n"
        "descriptionに「食品名 分量 Xkcal P:Xg F:Xg C:Xg 塩:Xg」の形式で記載すること。\n"
        "既にfood_defaultsに存在する場合はsave_food_defaultを呼ばない。\n"
    )


def _build_split_section(enabled: bool) -> str:
    """複数品目分割セクションを返す"""
    if not enabled:
        return ""
    return (
        "\n【複数品目の個別記録】\n"
        "複数の食品が1メッセージに含まれる場合、品目ごとに別々のrecord_mealを呼ぶこと。\n"
        "例：「朝食に白米と味噌汁」→ record_meal×2（白米、味噌汁を個別に記録）\n"
        "ただし「幕の内弁当」のようなセット商品は1件で記録する。\n"
    )


def _build_block1_text(user_name: str, height_cm: str, calorie_goal: str,
                       user_notes: str, search_flow: str,
                       auto_save: str, split: str, day_start_hour: int = 4) -> str:
    """Block 1（キャッシュ対象）のプロンプトテキストを構築"""
    notes_line = "\n- 注意事項: " + user_notes if user_notes else ""
    if day_start_hour > 0:
        hour_end = day_start_hour - 1
        day_boundary_rule = (
            f"- 日の区切りは午前{day_start_hour}時。0:00〜{hour_end}:59 の記録は前日として扱う\n"
        )
    else:
        day_boundary_rule = ""
    return f"""あなたは食事記録アシスタントです。ユーザー {user_name} の食事・体重・歩数・運動（消費カロリー）を記録します。

【ユーザー情報】
- 身長: {height_cm}cm / 1日の目標カロリー: {calorie_goal}kcal{notes_line}

【記録ルール】
- 日付指定なし → 今日の日付を使用（今日の日付は【セッション情報】を参照すること）
- 日付指定あり → 明示された日付を使用（例：「3月18日」→ 2026-03-18）
- 水分摂取量の報告 → mealsのnotesに記録
- 記録後は必ず日本語で確認メッセージを返す
- 食事区分が不明な場合は文脈から推定
- 修正依頼時は変更前・変更後の内容を確認メッセージに含める
- 食事と無関係な話題にも普通に日本語で応答する
- ユーザーが特定の日付に言及した場合（例：「3月15日の昼食」）、get_daily_summary ツールでDBから情報を取得してから回答すること
- 「朝食抜いた」「昼食べなかった」「夕食スキップ」などの報告時は record_meal_skip を呼ぶこと（間食・夜食は対象外）
- スキップ記録の訂正時は delete_meal_skip を呼ぶこと
- delete_meal_skip の結果で deleted=false の場合は「スキップ記録がありませんでした」と応答すること
- 「ランニングした」「ジムに行った」「30分歩いた」「筋トレした」など運動の報告時は record_exercise を呼ぶこと
- 消費カロリーは運動の種類・強度・時間・ユーザーの体重から推定する（体重が設定されている場合は参照）
- ユーザーが「消費カロリーは〇〇kcal」と明示した場合はその値をそのまま calories_burned に使用すること
- ユーザーに確認なく推定値で記録する（record_meal と同様の方針）
{day_boundary_rule}- 食事・体重記録後に日の合計を表示する際は、必ず get_daily_summary ツールを呼んでDBから取得した当日分のみを表示すること。会話履歴から以前の日付の食事を推測してサマリーに含めることを厳禁とする

【確認メッセージの形式】
✅ 朝食を記録しました
メニュー：白米150g、味噌汁
420kcal / P:10g F:5g C:80g 塩分:1.2g

{search_flow}

{auto_save}{split}
【量の換算ルール】
- 「150g食べた」＋「100gあたりXkcal」のデータ → ×1.5 で自動換算
- 「2個食べた」＋「1個あたりXkcal」→ ×2 で自動換算
- 「半分食べた」 → ×0.5 で自動換算
- 量の記載なし＋「100gあたり」のデータ → show_choices で確認

【食事提案】
ユーザーが食事の提案を求めた場合のみ提案する。
get_daily_summary で当日の記録を取得し、残りカロリーに基づいて3候補を提案する。

【関連食品情報について】
ユーザーメッセージに「【関連食品情報】」セクションが含まれる場合、その情報はユーザーが事前登録した食品データです。
入力内容と関連すると判断した場合のみ使用してください。関連しない場合は無視して構いません。

すべての返答を日本語で行うこと。"""


def build_system_prompt(savings_mode: bool = False, summary: str | None = None) -> list[dict]:
    """システムプロンプトを2ブロック構成で返す。
    Block 1（キャッシュ対象）: ユーザー設定・ルール（日付を含まない）
    Block 2（キャッシュ対象外）: 今日の日付・会話サマリー
    """
    today = database.get_logical_today_jst()
    logical_dt = datetime.strptime(today, "%Y-%m-%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][logical_dt.weekday()]

    calorie_goal = database.get_setting("daily_calorie_goal") or "1800"
    user_name = database.get_setting("user_name") or "DefaultName"
    height_cm = database.get_setting("user_height_cm") or "160"
    user_notes = database.get_setting("user_notes") or ""
    cache_ttl = database.get_setting("cache_ttl") or "5min"
    raw_hour = database.get_setting("day_start_hour") or "4"
    try:
        day_start_hour = int(raw_hour)
    except ValueError:
        logger.warning("day_start_hour の設定値が不正です: %s。デフォルト値 4 を使用します。", raw_hour)
        day_start_hour = 4

    if summary is None:
        summary = database.load_conversation_summary()
    summary_section = f"\n【会話サマリー（自動生成）】\n{summary}\n" if summary else ""

    cache_control: dict = (
        {"type": "ephemeral", "ttl": "1h"} if cache_ttl == "1hour"
        else {"type": "ephemeral"}
    )

    block1_text = _build_block1_text(
        user_name, height_cm, calorie_goal, user_notes,
        search_flow=_build_search_flow_section(savings_mode),
        auto_save=_build_auto_save_section(database.get_setting("auto_save_food_defaults") != "false"),
        split=_build_split_section(database.get_setting("split_multiple_items") == "true"),
        day_start_hour=day_start_hour,
    )

    block2_text = f"【セッション情報】\n今日の日付: {today}（{weekday}曜日）{summary_section}"

    return [
        {"type": "text", "text": block1_text, "cache_control": cache_control},
        {"type": "text", "text": block2_text},
    ]


# ── ツール実行 ─────────────────────────────────────────────────────────────────

MEAL_TYPE_JA = {
    "breakfast": "朝食",
    "lunch": "昼食",
    "dinner": "夕食",
    "snack": "間食",
    "late_night": "夜食",
}


def _tool_record_meal(inp: dict) -> dict:
    # meal_time: 明示なし × 当日 → 送信時刻(HH:MM)、過去日 → None のまま
    meal_time = inp.get("meal_time")
    if meal_time is None:
        today_str = database.get_logical_today_jst()
        meal_date = inp.get("meal_date", today_str)
        if meal_date == today_str:
            meal_time = datetime.now(JST).strftime("%H:%M")

    meal_id = database.save_meal(
        meal_date=inp["meal_date"],
        meal_type=inp["meal_type"],
        description=inp["description"],
        calories=inp.get("calories"),
        protein=inp.get("protein"),
        fat=inp.get("fat"),
        carbs=inp.get("carbs"),
        sodium=inp.get("sodium"),
        notes=inp.get("notes"),
        meal_time=meal_time,
    )
    return {
        "success": True,
        "tool": "record_meal",
        "meal_id": meal_id,
        "meal_type_ja": MEAL_TYPE_JA.get(inp["meal_type"], inp["meal_type"]),
        "description": inp["description"],
        "calories": inp.get("calories"),
        "protein": inp.get("protein"),
        "fat": inp.get("fat"),
        "carbs": inp.get("carbs"),
        "sodium": inp.get("sodium"),
        "image_source_type": inp.get("image_source_type", "photo"),
    }


def _tool_record_weight(inp: dict) -> dict:
    weight_kg = inp["weight_kg"]
    time_of_day = inp["time_of_day"]
    log_date = inp["log_date"]
    prev = database.get_previous_weight(time_of_day, log_date)
    weight_id = database.save_weight(log_date, time_of_day, weight_kg)
    delta = round(weight_kg - prev, 1) if prev is not None else None
    return {
        "success": True,
        "tool": "record_weight",
        "weight_id": weight_id,
        "weight_kg": weight_kg,
        "time_of_day": time_of_day,
        "time_of_day_ja": "朝" if time_of_day == "morning" else "夜",
        "previous_weight": prev,
        "delta": delta,
    }


def _tool_record_steps(inp: dict) -> dict:
    result = database.save_steps(inp["log_date"], inp["steps"])
    return {
        "success": True,
        "tool": "record_steps",
        "id": result["id"],
        "steps": inp["steps"],
        "updated": result["updated"],
        "previous_steps": result.get("previous_steps"),
    }


def _tool_get_daily_summary(inp: dict) -> dict:
    summary = database.get_daily_summary(inp.get("target_date"))
    return {"success": True, "tool": "get_daily_summary", "summary": summary}


def _tool_update_meal(inp: dict) -> dict:
    meal_id = inp["meal_id"]
    kwargs = {k: v for k, v in inp.items() if k != "meal_id"}
    ok = database.update_meal(meal_id, **kwargs)
    return {"success": ok, "tool": "update_meal", "meal_id": meal_id}


async def _tool_search_food_nutrition(inp: dict) -> dict:
    result = await food_search.search_nutrition(
        inp["food_name"],
        inp.get("amount", ""),
    )
    return {"success": True, "tool": "search_food_nutrition", **result}


def _tool_save_food_default(inp: dict) -> dict:
    database.save_food_default(
        keyword=inp["keyword"],
        description=inp["description"],
        notes=inp.get("notes"),
    )
    return {"success": True, "tool": "save_food_default", "keyword": inp["keyword"]}


def _tool_show_choices(inp: dict) -> dict:
    # 実際の表示はstream_chat側でSSEイベントとして送信する
    return {
        "success": True,
        "tool": "show_choices",
        "displayed": True,
        "question": inp.get("question", ""),
        "options": inp.get("options", []),
    }


def _tool_record_meal_skip(inp: dict) -> dict:
    database.save_meal_skip(inp["meal_date"], inp["meal_type"])
    return {
        "success": True,
        "tool": "record_meal_skip",
        "meal_date": inp["meal_date"],
        "meal_type": inp["meal_type"],
        "meal_type_ja": MEAL_TYPE_JA.get(inp["meal_type"], inp["meal_type"]),
    }


def _tool_delete_meal_skip(inp: dict) -> dict:
    deleted = database.delete_meal_skip(inp["meal_date"], inp["meal_type"])
    return {
        "success": True,
        "tool": "delete_meal_skip",
        "meal_date": inp["meal_date"],
        "meal_type": inp["meal_type"],
        "meal_type_ja": MEAL_TYPE_JA.get(inp["meal_type"], inp["meal_type"]),
        "deleted": deleted,
    }


# ── Phase 12 ハンドラー ─────────────────────────────────────────────────────────

def _tool_record_sleep(inp: dict) -> dict:
    result = database.upsert_sleep_log(
        inp["date"],
        inp["sleep_start"],
        inp["sleep_end"],
        inp.get("deep_minutes"),
        inp.get("rem_minutes"),
        inp.get("awake_minutes"),
        source="manual",
    )
    return {
        "success": True,
        "tool": "record_sleep",
        "date": inp["date"],
        "duration_minutes": result["duration_minutes"],
        "updated": result["updated"],
    }


def _tool_get_sleep_summary(inp: dict) -> dict:
    logs = database.get_sleep_logs(inp["start_date"], inp["end_date"])
    return {"success": True, "tool": "get_sleep_summary", "logs": logs}


def _tool_record_vital(inp: dict) -> dict:
    vital_type = inp.get("type")
    value = inp.get("value")
    if vital_type in ("heart_rate", "spo2") and value is None:
        return {
            "success": False,
            "tool": "record_vital",
            "error": f"type='{vital_type}' の記録には value（数値）が必要です。",
        }
    vid = database.insert_vital_log(
        inp["date"],
        vital_type,
        value,
        time=inp.get("time"),
        note=inp.get("note"),
        source="manual",
    )
    return {"success": True, "tool": "record_vital", "id": vid, "type": vital_type}


def _tool_get_vital_summary(inp: dict) -> dict:
    logs = database.get_vital_logs(inp["start_date"], inp["end_date"], inp.get("type"))
    return {"success": True, "tool": "get_vital_summary", "logs": logs}


def _tool_get_bmi_info(inp: dict) -> dict:
    info = database.get_latest_bmi_info()
    if info is None:
        return {
            "success": False,
            "tool": "get_bmi_info",
            "message": "体重の記録がないためBMIを計算できません。体重を記録してください。",
        }
    return {
        "success": True,
        "tool": "get_bmi_info",
        "bmi": info["bmi"],
        "bmi_status": info["bmi_status"],
        "bmr_kcal": info["bmr_kcal"],
        "bmr_note": info["bmr_note"],
        "weight_kg": info["weight_kg"],
        "height_cm": info["height_cm"],
        "log_date": info["log_date"],
    }


def _tool_record_exercise(inp: dict) -> dict:
    cal = int(inp["calories_burned"])
    if not (0 <= cal <= 9999):
        return {"success": False, "error": "calories_burned は 0〜9999 の範囲で指定してください"}
    desc = str(inp.get("description", ""))[:500]
    eid = database.save_exercise(
        inp["log_date"],
        cal,
        desc,
        source="chat",
    )
    return {
        "success": True,
        "tool": "record_exercise",
        "id": eid,
        "log_date": inp["log_date"],
        "calories_burned": cal,
        "description": desc,
    }


_TOOL_DISPATCH: dict = {
    "record_meal":           _tool_record_meal,
    "record_weight":         _tool_record_weight,
    "record_steps":          _tool_record_steps,
    "get_daily_summary":     _tool_get_daily_summary,
    "update_meal":           _tool_update_meal,
    "search_food_nutrition": _tool_search_food_nutrition,
    "save_food_default":     _tool_save_food_default,
    "show_choices":          _tool_show_choices,
    "record_meal_skip":      _tool_record_meal_skip,
    "delete_meal_skip":      _tool_delete_meal_skip,
    "record_sleep":          _tool_record_sleep,
    "get_sleep_summary":     _tool_get_sleep_summary,
    "record_vital":          _tool_record_vital,
    "get_vital_summary":     _tool_get_vital_summary,
    "get_bmi_info":          _tool_get_bmi_info,
    "record_exercise":       _tool_record_exercise,
}


async def execute_tool(name: str, input_data: dict) -> dict:
    handler = _TOOL_DISPATCH.get(name)
    if not handler:
        logger.warning("Unknown tool requested: %s", name)
        return {"success": False, "error": f"Unknown tool: {name}"}
    try:
        result = handler(input_data)
        if inspect.isawaitable(result):
            return await result
        return result
    except Exception as e:
        logger.error("Tool execution failed (tool=%s): %s", name, e, exc_info=True)
        return {"success": False, "tool": name, "error": str(e)}


# ── トークン監視・会話圧縮 ────────────────────────────────────────────────────

def _estimate_tokens(messages: list[dict], system_prompt: list[dict] | str) -> int:
    """入力トークン数を近似推定する（JSON文字数 ÷ 4）"""
    if isinstance(system_prompt, list):
        total_chars = sum(len(block.get("text", "")) for block in system_prompt)
    else:
        total_chars = len(system_prompt)
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        total_chars += len(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        total_chars += len(json.dumps(block.get("input", {})))
                    elif block.get("type") == "tool_result":
                        total_chars += len(str(block.get("content", "")))
                    elif block.get("type") == "image":
                        total_chars += 1000  # 画像は固定で1000トークン相当と見なす
    return total_chars // 4


async def _compress_history(
    client: anthropic.AsyncAnthropic,
    conversation_history: list[dict],
    keep_recent: int,
    savings_mode: bool,
) -> str:
    """
    古い会話履歴をHaikuで要約し、サマリー文字列を返す。
    圧縮後、conversation_historyから古いメッセージを削除する（in-place）。
    """
    if len(conversation_history) <= keep_recent:
        return ""

    old_messages = conversation_history[:-keep_recent]
    existing_summary = database.load_conversation_summary() or ""

    if savings_mode:
        instruction = "以下の会話を1〜3行の箇条書きで極めて簡潔に要約してください。記録済みの食事・体重・歩数の数値のみ残してください。"
    else:
        instruction = "以下の会話の重要情報（記録済みの食事・体重・歩数・ユーザーの好み・決定事項）を箇条書きで要約してください。"

    summary_prompt = instruction
    if existing_summary:
        summary_prompt += f"\n\n【既存のサマリー】\n{existing_summary}\n\n【追加する会話】\n"
    else:
        summary_prompt += "\n\n【会話内容】\n"

    for msg in old_messages:
        role_label = "ユーザー" if msg["role"] == "user" else "アシスタント"
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            content_str = " ".join(texts)
        else:
            content_str = str(content)
        if content_str.strip():
            summary_prompt += f"{role_label}: {content_str[:300]}\n"

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300 if savings_mode else 500,
            messages=[{"role": "user", "content": summary_prompt}],
        )
        new_summary = resp.content[0].text.strip()
    except Exception as e:
        logger.warning("会話圧縮に失敗（既存サマリーを使用）: %s", e)
        return existing_summary

    # 古いメッセージをin-placeで削除
    del conversation_history[:-keep_recent]

    # DBの古いメッセージも削除
    database.trim_conversation_history(keep_recent)

    # 新しいサマリーをDBに保存
    latest_id = database.get_latest_conversation_message_id()
    database.save_conversation_summary(new_summary, covered_up_to=latest_id)

    return new_summary


# ── ストリーミングチャット ──────────────────────────────────────────────────────

def get_client() -> anthropic.AsyncAnthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY") or database.get_setting("anthropic_api_key")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません")
    cache_ttl = database.get_setting("cache_ttl") or "5min"
    extra_headers: dict = {}
    if cache_ttl == "1hour":
        extra_headers["anthropic-beta"] = "extended-cache-ttl-2025-04-11"
    return anthropic.AsyncAnthropic(api_key=api_key, default_headers=extra_headers)


def _prepare_user_content(
    user_message: str,
    images: list[str],
) -> tuple[list, list[str]]:
    """画像処理＋テキストブロックからユーザーコンテンツを組み立て、処理済み画像リストを返す"""
    pending_images: list[str] = []
    user_content: list = []
    for img_b64 in images:
        try:
            processed = process_image_b64(img_b64)
        except Exception as e:
            logger.warning("画像処理に失敗（元データを使用）: %s", e)
            processed = img_b64
        pending_images.append(processed)
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": processed},
        })
    user_content.append({"type": "text", "text": user_message})
    return user_content, pending_images


def _inject_food_hints(conversation_history: list[dict], user_message: str) -> None:
    """food_defaults スマートマッチングを行い、最新メッセージにヒントを注入する（DBには保存しない）"""
    if database.get_setting("use_food_defaults") == "false":
        return
    all_fds = database.get_food_defaults()
    matched_fds = _match_food_defaults(user_message, all_fds)
    if not matched_fds:
        return
    fd_lines = "\n".join(
        f"- {fd['keyword']}: {fd['description']}" + (f"（{fd['notes']}）" if fd.get("notes") else "")
        for fd in matched_fds
    )
    fd_hint = "【関連食品情報（ユーザー登録済み）】\n" + fd_lines + "\n\n"
    for item in conversation_history[-1]["content"]:
        if item["type"] == "text":
            item["text"] = fd_hint + item["text"]
            break


async def _execute_tool_and_format(
    tc: dict,
    pending_images: list[str],
) -> tuple[dict, list[dict]]:
    """ツールを1件実行し、APIへ返す tool_result と SSE送信用イベントリストを返す。
    record_meal 成功時は pending_images を副作用でクリアする。
    """
    result = await execute_tool(tc["name"], tc["input"])
    sse_events: list[dict] = []

    if tc["name"] == "show_choices":
        api_result = {
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": json.dumps({"displayed": True}, ensure_ascii=False),
        }
        sse_events.append({
            "type": "choices",
            "question": result.get("question", ""),
            "options": result.get("options", []),
        })

    elif tc["name"] == "record_meal" and result.get("success"):
        meal_id = result["meal_id"]
        source_type = result.get("image_source_type", "photo")
        for img_b64 in pending_images:
            try:
                image_bytes = base64.b64decode(img_b64)
                rel_path = save_image_to_fs(image_bytes)
                database.save_meal_image_path(
                    meal_id=meal_id,
                    image_path=rel_path,
                    mime_type="image/jpeg",
                    source_type=source_type,
                )
            except Exception as e:
                logger.error("食事画像の保存に失敗 (meal_id=%s): %s", meal_id, e)
        pending_images.clear()
        api_result = {
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": json.dumps(result, ensure_ascii=False),
        }
        sse_events.append({"type": "record_done", "record_type": "meal", "record_id": meal_id})

    elif tc["name"] == "record_weight" and result.get("success"):
        api_result = {
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": json.dumps(result, ensure_ascii=False),
        }
        sse_events.append({"type": "record_done", "record_type": "weight", "record_id": result["weight_id"]})

    elif tc["name"] == "record_steps" and result.get("success"):
        api_result = {
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": json.dumps(result, ensure_ascii=False),
        }
        sse_events.append({"type": "record_done", "record_type": "steps", "record_id": result["id"]})

    elif tc["name"] == "record_meal_skip" and result.get("success"):
        api_result = {
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": json.dumps(result, ensure_ascii=False),
        }
        sse_events.append({"type": "record_done", "record_type": "meal_skip"})

    elif tc["name"] == "delete_meal_skip" and result.get("success"):
        api_result = {
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": json.dumps(result, ensure_ascii=False),
        }
        sse_events.append({"type": "record_done", "record_type": "meal_skip"})

    else:
        api_result = {
            "type": "tool_result",
            "tool_use_id": tc["id"],
            "content": json.dumps(result, ensure_ascii=False),
        }

    return api_result, sse_events


async def stream_chat(
    user_message: str,
    images: list[str],
    conversation_history: list[dict],
) -> AsyncGenerator[str, None]:
    """
    SSEイベント文字列をyieldする非同期ジェネレータ。

    イベント種別:
      {"type": "text",    "content": "..."}   テキストチャンク
      {"type": "choices", "question": "...", "options": [...]}  選択肢ボタン
      {"type": "done"}                         完了
      {"type": "error",   "message": "..."}    エラー
    """
    client = get_client()

    # 設定読み込み
    savings_mode = database.get_setting("savings_mode") == "true"
    token_compress_threshold = 8000 if savings_mode else 20000
    keep_recent = 3 if savings_mode else 10
    active_model = (
        database.get_setting("savings_model") or MODEL_SAVINGS
        if savings_mode else
        database.get_setting("normal_model") or MODEL
    )
    # 節約モードでは食品検索ツールを除外（Claude推定に切り替え）
    active_tools = [t for t in TOOLS if t["name"] != "search_food_nutrition"] if savings_mode else TOOLS

    # ユーザーメッセージ組み立て・DB保存
    # C-2: DB保存はヒント注入前に行う（json.dumpsで値がコピーされるため、後のインメモリ変更はDBに影響しない）
    user_content, pending_images = _prepare_user_content(user_message, images)
    conversation_history.append({"role": "user", "content": user_content})
    database.save_conversation_message("user", user_content)

    # food_defaults スマートマッチング（インメモリのみ）
    _inject_food_hints(conversation_history, user_message)

    # システムプロンプト / トークン超過時に会話圧縮
    system_prompt = build_system_prompt(savings_mode=savings_mode)
    estimated = _estimate_tokens(conversation_history, system_prompt)
    if estimated > token_compress_threshold:
        new_summary = await _compress_history(client, conversation_history, keep_recent, savings_mode)
        if new_summary:
            system_prompt = build_system_prompt(savings_mode=savings_mode, summary=new_summary)

    max_iterations = 10
    for _ in range(max_iterations):
        # リトライループ（429 レート制限 / 一時的なAPIエラー対応）
        _last_error: Exception | None = None
        for _attempt in range(MAX_API_RETRIES):
            collected_text = ""
            tool_calls: list[dict] = []
            current_tool: dict | None = None
            current_tool_json = ""

            try:
                async with client.messages.stream(
                    model=active_model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=conversation_history,
                    tools=active_tools,
                ) as stream:
                    async for event in stream:
                        etype = event.type

                        if etype == "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
                                current_tool = {"id": block.id, "name": block.name}
                                current_tool_json = ""

                        elif etype == "content_block_delta":
                            delta = event.delta
                            if delta.type == "text_delta":
                                collected_text += delta.text
                                yield f"data: {json.dumps({'type': 'text', 'content': delta.text}, ensure_ascii=False)}\n\n"
                            elif delta.type == "input_json_delta" and current_tool is not None:
                                current_tool_json += delta.partial_json

                        elif etype == "content_block_stop":
                            if current_tool is not None:
                                try:
                                    current_tool["input"] = json.loads(current_tool_json) if current_tool_json else {}
                                except json.JSONDecodeError:
                                    current_tool["input"] = {}
                                tool_calls.append(current_tool)
                                current_tool = None
                                current_tool_json = ""

                _last_error = None
                break  # 成功したらリトライループを抜ける

            except anthropic.RateLimitError as e:
                _last_error = e
                if _attempt < MAX_API_RETRIES - 1:
                    wait_sec = 2 ** _attempt
                    _msg = '⏳ APIが混み合っています。' + str(wait_sec) + '秒後に再試行します…\n'
                    yield f"data: {json.dumps({'type': 'text', 'content': _msg}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(wait_sec)

            except Exception as e:
                logger.error("ストリーミング中に予期しないエラー: %s", e, exc_info=True)
                yield f"data: {json.dumps({'type': 'error', 'message': _format_error_message(e)}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

        if _last_error is not None:
            yield f"data: {json.dumps({'type': 'error', 'message': _format_error_message(_last_error)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # アシスタントメッセージを会話履歴に追記
        assistant_content: list = []
        if collected_text:
            assistant_content.append({"type": "text", "text": collected_text})
        for tc in tool_calls:
            assistant_content.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"],
            })
        if assistant_content:
            conversation_history.append({"role": "assistant", "content": assistant_content})
            database.save_conversation_message("assistant", assistant_content)

        if not tool_calls:
            break

        # ツール実行・SSEイベント送信
        tool_results: list = []
        for tc in tool_calls:
            api_result, sse_events = await _execute_tool_and_format(tc, pending_images)
            tool_results.append(api_result)
            for evt in sse_events:
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"

        conversation_history.append({"role": "user", "content": tool_results})
        database.save_conversation_message("user", tool_results)

    yield f"data: {json.dumps({'type': 'done'})}\n\n"

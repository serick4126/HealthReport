import asyncio
import base64
import json
import os
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator

import anthropic

import database
import food_search
from image_utils import process_image_b64

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
]


# ── システムプロンプト ──────────────────────────────────────────────────────────

def build_system_prompt(savings_mode: bool = False, summary: str | None = None) -> list[dict]:
    """
    システムプロンプトを2ブロック構成で返す。
    Block 1（キャッシュ対象）: ユーザー設定・ルール・food_defaults（日付を含まない）
    Block 2（キャッシュ対象外）: 今日の日付・会話サマリー（毎日/セッションごとに変化）
    """
    now_jst = datetime.now(JST)
    today = now_jst.date().isoformat()
    weekday = ["月", "火", "水", "木", "金", "土", "日"][now_jst.weekday()]

    calorie_goal = database.get_setting("daily_calorie_goal") or "1800"
    user_name = database.get_setting("user_name") or "DefaultName"
    height_cm = database.get_setting("user_height_cm") or "160"
    user_notes = database.get_setting("user_notes") or ""
    cache_ttl = database.get_setting("cache_ttl") or "5min"

    # 節約モードでは food_defaults をシステムプロンプトに含めない
    if savings_mode:
        fd_section = ""
    else:
        food_defaults = database.get_food_defaults()
        fd_lines = "\n".join(
            f"- {fd['keyword']}: {fd['description']}" + (f"（{fd['notes']}）" if fd.get("notes") else "")
            for fd in food_defaults
        ) if food_defaults else "（設定なし）"
        fd_section = f"\n【食品デフォルト設定】\n以下の食品が報告された場合、設定値を自動適用（表記ゆれも対応）:\n{fd_lines}\n"

    # 会話サマリー（圧縮済み履歴）
    if summary is None:
        summary = database.load_conversation_summary()
    summary_section = f"\n【会話サマリー（自動生成）】\n{summary}\n" if summary else ""

    # 節約モードでは食品検索せず推定に切り替え
    if savings_mode:
        search_flow_section = "（節約モード）すべての食品はClaudeの知識で直接推定してrecord_mealを呼ぶこと。search_food_nutritionは使用しない。notesに '[推定値] カロリー・PFCはClaudeによる推定' を記載すること。"
    else:
        search_flow_section = (
            "ブランド品・パッケージ食品・飲料が報告された場合：\n"
            "1. search_food_nutrition で検索する\n"
            "2. 複数候補 → show_choices で表示（必ず最後に「\U0001f50d 該当する商品がない」を追加）\n"
            "3. 1候補のみ → そのまま採用して record_meal を呼ぶ\n"
            "4. 0候補（見つからない）→ Claudeの知識で推定して自動的に record_meal を呼ぶ。ユーザーに確認しない。\n"
            "   notesに '[推定値] カロリー・PFCはClaudeによる推定' を記載すること。\n"
            "鶏むね肉・白米・卵などの一般食材はClaudeの知識で推定してよい。\n"
            "show_choicesを呼び出した後は余分なテキストを出力しないこと。"
        )

    # cache_control（TTL設定に応じて切り替え）
    if cache_ttl == "1hour":
        cache_control: dict = {"type": "ephemeral", "ttl": "1h"}
    else:
        cache_control = {"type": "ephemeral"}

    auto_save_fd = database.get_setting("auto_save_food_defaults") != "false"
    if auto_save_fd:
        auto_save_section = (
            "\n【food_defaults自動保存】\n"
            "推定値([推定値])またはSlism検索([Slism])でrecord_mealが成功した場合、\n"
            "food_defaultsに同じキーワードが未登録であればsave_food_defaultを呼んで自動保存すること。\n"
            "保存するキーワードは食品の主要名（例：「ヤクルト1000」「コーヒー牛乳」）とし、\n"
            "descriptionに「食品名 分量 Xkcal P:Xg F:Xg C:Xg 塩:Xg」の形式で記載すること。\n"
            "既にfood_defaultsに存在する場合はsave_food_defaultを呼ばない。\n"
        )
    else:
        auto_save_section = ""

    user_notes_line = "\n- 注意事項: " + user_notes if user_notes else ""

    # ── Block 1: キャッシュ対象 ──────────────────────────────────────────────
    # 日付・サマリーを含まないため、設定変更がない限り日をまたいでもキャッシュヒットする
    block1_text = f"""あなたは食事記録アシスタントです。ユーザー {user_name} の食事・体重・歩数を記録します。

【ユーザー情報】
- 身長: {height_cm}cm / 1日の目標カロリー: {calorie_goal}kcal{user_notes_line}

【記録ルール】
- 日付指定なし → 今日の日付を使用（今日の日付は【セッション情報】を参照すること）
- 日付指定あり → 明示された日付を使用（例：「3月18日」→ 2026-03-18）
- 水分摂取量の報告 → mealsのnotesに記録
- 記録後は必ず日本語で確認メッセージを返す
- 食事区分が不明な場合は文脈から推定
- 修正依頼時は変更前・変更後の内容を確認メッセージに含める
- 食事と無関係な話題にも普通に日本語で応答する
- ユーザーが特定の日付に言及した場合（例：「3月15日の昼食」）、get_daily_summary ツールでDBから情報を取得してから回答すること

【確認メッセージの形式】
✅ 朝食を記録しました
メニュー：白米150g、味噌汁
420kcal / P:10g F:5g C:80g 塩分:1.2g

{search_flow_section}

{auto_save_section}
【量の換算ルール】
- 「150g食べた」＋「100gあたりXkcal」のデータ → ×1.5 で自動換算
- 「2個食べた」＋「1個あたりXkcal」→ ×2 で自動換算
- 「半分食べた」 → ×0.5 で自動換算
- 量の記載なし＋「100gあたり」のデータ → show_choices で確認

【食事提案】
ユーザーが食事の提案を求めた場合のみ提案する。
get_daily_summary で当日の記録を取得し、残りカロリーに基づいて3候補を提案する。
{fd_section}
すべての返答を日本語で行うこと。"""

    # ── Block 2: キャッシュ対象外 ────────────────────────────────────────────
    # 今日の日付とサマリーのみ。毎日変化するためキャッシュしない。
    block2_text = f"""【セッション情報】
今日の日付: {today}（{weekday}曜日）{summary_section}"""

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


async def execute_tool(name: str, input_data: dict) -> dict:
    try:
        if name == "record_meal":
            meal_id = database.save_meal(
                meal_date=input_data["meal_date"],
                meal_type=input_data["meal_type"],
                description=input_data["description"],
                calories=input_data.get("calories"),
                protein=input_data.get("protein"),
                fat=input_data.get("fat"),
                carbs=input_data.get("carbs"),
                sodium=input_data.get("sodium"),
                notes=input_data.get("notes"),
            )
            return {
                "success": True,
                "tool": "record_meal",
                "meal_id": meal_id,
                "meal_type_ja": MEAL_TYPE_JA.get(input_data["meal_type"], input_data["meal_type"]),
                "description": input_data["description"],
                "calories": input_data.get("calories"),
                "protein": input_data.get("protein"),
                "fat": input_data.get("fat"),
                "carbs": input_data.get("carbs"),
                "sodium": input_data.get("sodium"),
                "image_source_type": input_data.get("image_source_type", "photo"),
            }

        elif name == "record_weight":
            weight_kg = input_data["weight_kg"]
            time_of_day = input_data["time_of_day"]
            log_date = input_data["log_date"]
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

        elif name == "record_steps":
            result = database.save_steps(input_data["log_date"], input_data["steps"])
            return {
                "success": True,
                "tool": "record_steps",
                "steps": input_data["steps"],
                "updated": result["updated"],
                "previous_steps": result.get("previous_steps"),
            }

        elif name == "get_daily_summary":
            summary = database.get_daily_summary(input_data.get("target_date"))
            return {"success": True, "tool": "get_daily_summary", "summary": summary}

        elif name == "update_meal":
            meal_id = input_data["meal_id"]
            kwargs = {k: v for k, v in input_data.items() if k != "meal_id"}
            ok = database.update_meal(meal_id, **kwargs)
            return {"success": ok, "tool": "update_meal", "meal_id": meal_id}

        elif name == "search_food_nutrition":
            result = await food_search.search_nutrition(
                input_data["food_name"],
                input_data.get("amount", ""),
            )
            return {"success": True, "tool": "search_food_nutrition", **result}

        elif name == "save_food_default":
            database.save_food_default(
                keyword=input_data["keyword"],
                description=input_data["description"],
                notes=input_data.get("notes"),
            )
            return {"success": True, "tool": "save_food_default", "keyword": input_data["keyword"]}

        elif name == "show_choices":
            # 実際の表示はstream_chat側でSSEイベントとして送信する
            return {
                "success": True,
                "tool": "show_choices",
                "displayed": True,
                "question": input_data.get("question", ""),
                "options": input_data.get("options", []),
            }

        else:
            return {"success": False, "error": f"Unknown tool: {name}"}

    except Exception as e:
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
    except Exception:
        return existing_summary  # 圧縮失敗時は既存サマリーをそのまま返す

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

    # 今回のリクエストで添付された画像（record_meal後にDB保存する）
    pending_images: list[str] = []  # 処理済みbase64

    # ユーザーメッセージを会話履歴に追加
    user_content: list = []
    for img_b64 in images:
        try:
            processed = process_image_b64(img_b64)
        except Exception:
            processed = img_b64
        pending_images.append(processed)
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": processed},
        })
    user_content.append({"type": "text", "text": user_message})
    conversation_history.append({"role": "user", "content": user_content})
    database.save_conversation_message("user", user_content)

    system_prompt = build_system_prompt(savings_mode=savings_mode)

    # トークン数チェック → 閾値超過で会話圧縮
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

        # ── ツール実行 ────────────────────────────────────────────────────────────
        tool_results: list = []
        choices_event: dict | None = None  # show_choices が呼ばれた場合のデータ

        for tc in tool_calls:
            result = await execute_tool(tc["name"], tc["input"])

            if tc["name"] == "show_choices":
                # フロントエンドへ choices イベントを送信（会話は継続）
                choices_event = {
                    "type": "choices",
                    "question": result.get("question", ""),
                    "options": result.get("options", []),
                }
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": json.dumps({"displayed": True}, ensure_ascii=False),
                })

            elif tc["name"] == "record_meal" and result.get("success"):
                # 画像が添付されていた場合、meal_images テーブルに保存
                meal_id = result["meal_id"]
                source_type = result.get("image_source_type", "photo")
                for img_b64 in pending_images:
                    try:
                        database.save_meal_image(
                            meal_id=meal_id,
                            image_data=base64.b64decode(img_b64),
                            mime_type="image/jpeg",
                            source_type=source_type,
                        )
                    except Exception:
                        pass
                pending_images.clear()
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

        conversation_history.append({"role": "user", "content": tool_results})
        database.save_conversation_message("user", tool_results)

        # choices イベントをフロントエンドへ送信
        if choices_event:
            yield f"data: {json.dumps(choices_event, ensure_ascii=False)}\n\n"

    yield f"data: {json.dumps({'type': 'done'})}\n\n"

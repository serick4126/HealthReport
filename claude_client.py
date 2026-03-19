import json
import os
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator

JST = timezone(timedelta(hours=9))

import anthropic

import database
from image_utils import process_image_b64

MODEL = "claude-sonnet-4-6"

# ── ツール定義 ─────────────────────────────────────────────────────────────────

TOOLS: list[anthropic.types.ToolParam] = [
    {
        "name": "record_meal",
        "description": "食事記録をデータベースに保存します。カロリー・PFC・塩分はClaudeの知識で推定して必ず入力してください。",
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_date": {
                    "type": "string",
                    "description": "食事の日付 (YYYY-MM-DD形式)",
                },
                "meal_type": {
                    "type": "string",
                    "enum": ["breakfast", "lunch", "dinner", "snack", "late_night"],
                    "description": "食事区分: breakfast=朝食, lunch=昼食, dinner=夕食, snack=間食, late_night=夜食",
                },
                "description": {
                    "type": "string",
                    "description": "食事内容の説明（例：オートミール50g、ヤクルト1000）",
                },
                "calories": {"type": "integer", "description": "カロリー (kcal)"},
                "protein": {"type": "number", "description": "タンパク質 (g)"},
                "fat": {"type": "number", "description": "脂質 (g)"},
                "carbs": {"type": "number", "description": "炭水化物・糖質 (g)"},
                "sodium": {"type": "number", "description": "塩分相当量 (g)"},
                "notes": {
                    "type": "string",
                    "description": "備考（推定値の場合は '[推定値] カロリー・PFCはClaudeによる推定' を含める）",
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
                "target_date": {
                    "type": "string",
                    "description": "対象日 (YYYY-MM-DD形式)。省略時は今日。",
                },
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
                "meal_type": {
                    "type": "string",
                    "enum": ["breakfast", "lunch", "dinner", "snack", "late_night"],
                },
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
]


# ── システムプロンプト ──────────────────────────────────────────────────────────

def build_system_prompt() -> str:
    now_jst = datetime.now(JST)
    today = now_jst.date().isoformat()
    weekday = ["月", "火", "水", "木", "金", "土", "日"][now_jst.weekday()]

    calorie_goal = database.get_setting("daily_calorie_goal") or "1500"
    user_name = database.get_setting("user_name") or "Serick"
    height_cm = database.get_setting("user_height_cm") or "180"

    food_defaults = database.get_food_defaults()
    if food_defaults:
        fd_lines = "\n".join(
            f"- {fd['keyword']}: {fd['description']}"
            + (f"（{fd['notes']}）" if fd.get("notes") else "")
            for fd in food_defaults
        )
    else:
        fd_lines = "（設定なし）"

    return f"""あなたは食事記録アシスタントです。ユーザー {user_name} の食事・体重・歩数を記録します。

【今日の情報】
今日の日付: {today}（{weekday}曜日）

【ユーザー情報】
- 身長: {height_cm}cm
- 1日の目標カロリー: {calorie_goal}kcal
- 主な注意事項: インスリン抵抗性・メタボリックシンドローム / 1日1500kcal・塩分管理・水分3L/日（糖分なし）

【記録ルール】
- 日付指定なし → 今日（{today}）の日付を使用
- 日付指定あり → 明示された日付を使用（例：「3月18日」→ 2026-03-18）
- カロリー・PFC・塩分は**必ずClaudeの知識で推定して**record_mealに渡す（0は入れない）
- 推定値の場合はnotesに「[推定値] カロリー・PFCはClaudeによる推定」を追記
- 水分摂取量の報告 → mealsのnotesに記録（例：「お茶500ml摂取」）
- 記録後は必ず日本語で確認メッセージを返す
- 食事区分が不明な場合は文脈から推定（例：「朝ご飯」→ breakfast）
- 皮あり/なし・量不明など、カロリーに大きく影響する情報が不明な場合はユーザーに確認する
- 修正依頼時は変更前・変更後の内容を確認メッセージに含める
- 食事・体重・歩数と無関係な話題にも普通に日本語で応答する

【確認メッセージの形式】
✅ 朝食を記録しました
メニュー：オートミール50g、ヤクルト1000
220kcal / P:8g F:4g C:38g 塩分:0.2g

【食事提案】
ユーザーが食事の提案を求めた場合のみ提案する。
get_daily_summary で当日の記録を取得し、残りカロリーに基づいて3候補を提案する。

【食品デフォルト設定】
以下の食品が報告された場合、設定値を自動適用（表記ゆれも対応）:
{fd_lines}

すべての返答を日本語で行うこと。"""


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
            }

        elif name == "record_weight":
            weight_kg = input_data["weight_kg"]
            time_of_day = input_data["time_of_day"]
            log_date = input_data["log_date"]

            prev = database.get_previous_weight(time_of_day, log_date)
            weight_id = database.save_weight(log_date, time_of_day, weight_kg)

            delta = None
            if prev is not None:
                delta = round(weight_kg - prev, 1)

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
            log_date = input_data["log_date"]
            steps = input_data["steps"]
            result = database.save_steps(log_date, steps)
            return {
                "success": True,
                "tool": "record_steps",
                "steps": steps,
                "updated": result["updated"],
                "previous_steps": result.get("previous_steps"),
            }

        elif name == "get_daily_summary":
            target_date = input_data.get("target_date")
            summary = database.get_daily_summary(target_date)
            return {"success": True, "tool": "get_daily_summary", "summary": summary}

        elif name == "update_meal":
            meal_id = input_data["meal_id"]
            kwargs = {k: v for k, v in input_data.items() if k != "meal_id"}
            ok = database.update_meal(meal_id, **kwargs)
            return {"success": ok, "tool": "update_meal", "meal_id": meal_id}

        else:
            return {"success": False, "error": f"Unknown tool: {name}"}

    except Exception as e:
        return {"success": False, "tool": name, "error": str(e)}


# ── ストリーミングチャット ──────────────────────────────────────────────────────

def get_client() -> anthropic.AsyncAnthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY") or database.get_setting("anthropic_api_key")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません")
    return anthropic.AsyncAnthropic(api_key=api_key)


async def stream_chat(
    user_message: str,
    images: list[str],
    conversation_history: list[dict],
) -> AsyncGenerator[str, None]:
    """
    ユーザーメッセージを受け取り、SSEイベント文字列をyieldする非同期ジェネレータ。
    - {"type": "text", "content": "..."}  テキストチャンク
    - {"type": "done"}                    完了
    - {"type": "error", "message": "..."}  エラー
    """
    client = get_client()

    # ユーザーメッセージを会話履歴に追加
    user_content: list = []
    for img_b64 in images:
        try:
            processed = process_image_b64(img_b64)
        except Exception:
            processed = img_b64  # 変換失敗時はそのまま送信
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": processed},
        })
    user_content.append({"type": "text", "text": user_message})
    conversation_history.append({"role": "user", "content": user_content})

    system_prompt = build_system_prompt()

    max_iterations = 8
    for _ in range(max_iterations):
        collected_text = ""
        tool_calls: list[dict] = []
        current_tool: dict | None = None
        current_tool_json = ""

        try:
            async with client.messages.stream(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                messages=conversation_history,
                tools=TOOLS,
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

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
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

        # ツール呼び出しがなければ終了
        if not tool_calls:
            break

        # ツールを実行して結果を会話履歴に追加
        tool_results: list = []
        for tc in tool_calls:
            result = await execute_tool(tc["name"], tc["input"])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })

        conversation_history.append({"role": "user", "content": tool_results})

    yield f"data: {json.dumps({'type': 'done'})}\n\n"

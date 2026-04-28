import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch
from datetime import datetime as real_datetime

from claude_client import _build_send_context


def _make_summary(meals=None, weight=None, steps=None, body_fat=None,
                  skipped=None, totals=None):
    return {
        "date": "2026-04-28",
        "meals": meals or [],
        "weight": weight or {},
        "steps": steps,
        "body_fat": body_fat,
        "skipped_meal_types": skipped or [],
        "totals": totals or {
            "calories": 0, "protein": 0.0, "fat": 0.0,
            "carbs": 0.0, "sodium": 0.0,
        },
    }


# ── 記録なし ────────────────────────────────────────────────────────────────

def test_build_send_context_no_records_shows_no_record():
    """記録が一切ない場合に「記録なし」が表示されること"""
    with patch("claude_client.database.get_daily_summary", return_value=_make_summary()), \
         patch("claude_client.database.get_exercise_logs", return_value=[]):
        result = _build_send_context("2026-04-28", "09:00", 1800)
    assert "記録なし" in result


def test_build_send_context_header_contains_date_and_time():
    """ヘッダーに論理日付と送信時刻が含まれること"""
    with patch("claude_client.database.get_daily_summary", return_value=_make_summary()), \
         patch("claude_client.database.get_exercise_logs", return_value=[]):
        result = _build_send_context("2026-04-28", "23:45", 1800)
    assert "論理日付: 2026-04-28" in result
    assert "23:45" in result
    assert "day_start_hour設定反映済み" in result


# ── 食事区分の3状態 ──────────────────────────────────────────────────────────

def test_build_send_context_meal_recorded_shows_kcal():
    """食事記録がある区分に XXXkcal が表示されること"""
    meals = [{"meal_type": "lunch", "calories": 500,
              "protein": 20.0, "fat": 15.0, "carbs": 70.0, "sodium": 1.5}]
    totals = {"calories": 500, "protein": 20.0, "fat": 15.0,
              "carbs": 70.0, "sodium": 1.5}
    with patch("claude_client.database.get_daily_summary",
               return_value=_make_summary(meals=meals, totals=totals)), \
         patch("claude_client.database.get_exercise_logs", return_value=[]):
        result = _build_send_context("2026-04-28", "13:00", 1800)
    assert "昼食: 500kcal" in result


def test_build_send_context_skipped_meal_shows_skip():
    """スキップ登録がある区分に「スキップ」が表示されること"""
    with patch("claude_client.database.get_daily_summary",
               return_value=_make_summary(skipped=["breakfast"])), \
         patch("claude_client.database.get_exercise_logs", return_value=[]):
        result = _build_send_context("2026-04-28", "09:00", 1800)
    assert "朝食: スキップ" in result


def test_build_send_context_unrecorded_meal_shows_unrecorded():
    """記録もスキップもない区分に「未記録」が表示されること"""
    with patch("claude_client.database.get_daily_summary",
               return_value=_make_summary(skipped=["breakfast"])), \
         patch("claude_client.database.get_exercise_logs", return_value=[]):
        result = _build_send_context("2026-04-28", "09:00", 1800)
    assert "夕食: 未記録" in result


# ── カロリー計算 ──────────────────────────────────────────────────────────────

def test_build_send_context_remaining_calories():
    """残りカロリーが 目標 - 摂取合計 で計算されること"""
    meals = [{"meal_type": "breakfast", "calories": 500,
              "protein": 10.0, "fat": 5.0, "carbs": 80.0, "sodium": 1.0}]
    totals = {"calories": 500, "protein": 10.0, "fat": 5.0,
              "carbs": 80.0, "sodium": 1.0}
    with patch("claude_client.database.get_daily_summary",
               return_value=_make_summary(meals=meals, totals=totals)), \
         patch("claude_client.database.get_exercise_logs", return_value=[]):
        result = _build_send_context("2026-04-28", "08:00", 1800)
    assert "残り 1300kcal" in result


# ── バイタル行 ────────────────────────────────────────────────────────────────

def test_build_send_context_vitals_omitted_when_no_records():
    """体重・歩数等が全て未記録の場合にバイタル行が省略されること"""
    meals = [{"meal_type": "lunch", "calories": 400,
              "protein": 20.0, "fat": 10.0, "carbs": 60.0, "sodium": 1.0}]
    totals = {"calories": 400, "protein": 20.0, "fat": 10.0,
              "carbs": 60.0, "sodium": 1.0}
    with patch("claude_client.database.get_daily_summary",
               return_value=_make_summary(meals=meals, totals=totals)), \
         patch("claude_client.database.get_exercise_logs", return_value=[]):
        result = _build_send_context("2026-04-28", "12:00", 1800)
    assert "体重:" not in result
    assert "歩数:" not in result


def test_build_send_context_exercise_summed():
    """複数運動記録がある場合に calories_burned の合計が表示されること"""
    with patch("claude_client.database.get_daily_summary",
               return_value=_make_summary()), \
         patch("claude_client.database.get_exercise_logs", return_value=[
             {"calories_burned": 150, "description": "ランニング"},
             {"calories_burned": 80, "description": "筋トレ"},
         ]):
        result = _build_send_context("2026-04-28", "20:00", 1800)
    assert "運動消費: 230kcal" in result


def test_build_send_context_weight_morning_only():
    """朝体重のみ記録の場合の表示を確認"""
    with patch("claude_client.database.get_daily_summary",
               return_value=_make_summary(weight={"morning": 65.2})), \
         patch("claude_client.database.get_exercise_logs", return_value=[]):
        result = _build_send_context("2026-04-28", "07:00", 1800)
    assert "体重: 朝 65.2kg" in result


def test_build_send_context_weight_evening_only():
    """夜体重のみ記録の場合の表示を確認"""
    with patch("claude_client.database.get_daily_summary",
               return_value=_make_summary(weight={"evening": 64.5})), \
         patch("claude_client.database.get_exercise_logs", return_value=[]):
        result = _build_send_context("2026-04-28", "22:00", 1800)
    assert "体重: 夜 64.5kg" in result


def test_build_send_context_same_meal_type_summed():
    """同一食事区分に複数レコードがある場合にkcalが合計されること"""
    meals = [
        {"meal_type": "lunch", "calories": 300,
         "protein": 10.0, "fat": 5.0, "carbs": 50.0, "sodium": 1.0},
        {"meal_type": "lunch", "calories": 200,
         "protein": 8.0, "fat": 3.0, "carbs": 30.0, "sodium": 0.5},
    ]
    totals = {"calories": 500, "protein": 18.0, "fat": 8.0,
              "carbs": 80.0, "sodium": 1.5}
    with patch("claude_client.database.get_daily_summary",
               return_value=_make_summary(meals=meals, totals=totals)), \
         patch("claude_client.database.get_exercise_logs", return_value=[]):
        result = _build_send_context("2026-04-28", "13:00", 1800)
    assert "昼食: 500kcal" in result


from claude_client import _inject_send_context, _build_block1_text, _build_search_flow_section
from claude_client import build_system_prompt


MOCK_SETTINGS = {
    "daily_calorie_goal": "1800",
    "user_name": "TestUser",
    "user_height_cm": "165",
    "user_notes": "",
    "cache_ttl": "5min",
    "auto_save_food_defaults": "true",
    "split_multiple_items": "false",
    "savings_mode": "false",
}


def _mock_get_setting(key):
    return MOCK_SETTINGS.get(key)


def test_build_system_prompt_no_block2_when_no_summary():
    """会話サマリーがない場合に Block 2 が省略されること（返り値が1要素）"""
    with patch("claude_client.database.get_setting", side_effect=_mock_get_setting), \
         patch("claude_client.database.load_conversation_summary", return_value=None):
        result = build_system_prompt(savings_mode=False, summary=None)
    assert len(result) == 1


def test_build_system_prompt_block2_when_summary_exists():
    """会話サマリーがある場合に Block 2 が出力されること（返り値が2要素）"""
    with patch("claude_client.database.get_setting", side_effect=_mock_get_setting), \
         patch("claude_client.database.load_conversation_summary", return_value="要約テキスト"):
        result = build_system_prompt(savings_mode=False, summary=None)
    assert len(result) == 2
    assert "要約テキスト" in result[1]["text"]


def test_build_system_prompt_block2_no_date():
    """Block 2 に今日の日付が含まれないこと"""
    with patch("claude_client.database.get_setting", side_effect=_mock_get_setting), \
         patch("claude_client.database.load_conversation_summary", return_value="要約テキスト"):
        result = build_system_prompt(savings_mode=False, summary=None)
    assert len(result) == 2
    import re
    assert not re.search(r"\d{4}-\d{2}-\d{2}", result[1]["text"])


def test_build_system_prompt_explicit_summary_used():
    """summary 引数を渡した場合は DB を参照せずその値が使われること"""
    with patch("claude_client.database.get_setting", side_effect=_mock_get_setting), \
         patch("claude_client.database.load_conversation_summary") as mock_load:
        result = build_system_prompt(savings_mode=False, summary="渡されたサマリー")
    mock_load.assert_not_called()
    assert len(result) == 2
    assert "渡されたサマリー" in result[1]["text"]


def _make_block1():
    return _build_block1_text(
        user_name="TestUser",
        height_cm="165",
        calorie_goal="1800",
        user_notes="",
        search_flow=_build_search_flow_section(False),
        auto_save="",
        split="",
    )


def test_block1_no_session_info_date_rule():
    """Block 1 に「セッション情報を参照すること」が含まれないこと"""
    text = _make_block1()
    assert "セッション情報を参照" not in text


def test_block1_no_day_boundary_rule():
    """Block 1 に「日の区切りは午前」が含まれないこと"""
    text = _make_block1()
    assert "日の区切りは午前" not in text


def test_block1_contains_send_context_section():
    """Block 1 に送信コンテキストの説明が含まれること"""
    text = _make_block1()
    assert "【送信コンテキスト】" in text


def test_block1_label_name_corrected():
    """Block 1 に「関連食品情報（ユーザー登録済み）」が含まれること"""
    text = _make_block1()
    assert "【関連食品情報（ユーザー登録済み）】" in text


def test_block1_simplified_daily_summary_rule():
    """Block 1 の記録後合計ルールが簡略化されていること"""
    text = _make_block1()
    assert "会話履歴から以前の日付の食事を推測してサマリーに含めることを厳禁とする" not in text
    assert "get_daily_summary" in text


# ── _inject_send_context ────────────────────────────────────────────────────

def test_inject_send_context_prepends_to_message():
    """送信コンテキストがユーザーメッセージの先頭に挿入されること"""
    history = [{"role": "user", "content": [{"type": "text", "text": "テスト"}]}]
    with patch("claude_client._build_send_context", return_value="【送信コンテキスト】TEST"), \
         patch("claude_client.database.get_logical_today_jst", return_value="2026-04-28"), \
         patch("claude_client.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "09:00"
        mock_dt.strptime.side_effect = real_datetime.strptime
        _inject_send_context(history, 1800)
    text = history[0]["content"][0]["text"]
    assert text.startswith("【送信コンテキスト】TEST")
    assert "テスト" in text


def test_inject_send_context_comes_before_food_hints():
    """food_hints が既注入でも送信コンテキストがその前に来ること"""
    hint = "【関連食品情報（ユーザー登録済み）】\nヤクルト: 47kcal\n\n"
    history = [{"role": "user", "content": [{"type": "text", "text": hint + "ヤクルト飲んだ"}]}]
    with patch("claude_client._build_send_context", return_value="【送信コンテキスト】TEST"), \
         patch("claude_client.database.get_logical_today_jst", return_value="2026-04-28"), \
         patch("claude_client.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "09:00"
        mock_dt.strptime.side_effect = real_datetime.strptime
        _inject_send_context(history, 1800)
    text = history[0]["content"][0]["text"]
    assert text.index("【送信コンテキスト】") < text.index("【関連食品情報（ユーザー登録済み）】")


def test_inject_send_context_only_text_block_modified():
    """image ブロックが含まれていてもテキストブロックのみ変更されること"""
    history = [{"role": "user", "content": [
        {"type": "image", "source": {"data": "base64=="}},
        {"type": "text", "text": "食事の写真"},
    ]}]
    with patch("claude_client._build_send_context", return_value="【送信コンテキスト】TEST"), \
         patch("claude_client.database.get_logical_today_jst", return_value="2026-04-28"), \
         patch("claude_client.datetime") as mock_dt:
        mock_dt.now.return_value.strftime.return_value = "09:00"
        mock_dt.strptime.side_effect = real_datetime.strptime
        _inject_send_context(history, 1800)
    assert history[0]["content"][0]["type"] == "image"
    assert "【送信コンテキスト】TEST" in history[0]["content"][1]["text"]

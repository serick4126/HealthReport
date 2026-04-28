"""Phase 25 Step 5 テスト — フォーカス設定をAIコメント生成に適用"""
import sys
import os
import tempfile
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

# database モジュールを一時DBで隔離
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

sys.path.insert(0, str(Path(__file__).parent.parent))
import database
database.DB_PATH = Path(_tmp.name)

import report_generator
from report_generator import _build_comment_prompt


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------
def teardown_module(_):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(_tmp.name + suffix)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# database.get_blood_pressure_range / get_body_fat_range
# ---------------------------------------------------------------------------
class TestNewDbFunctions:
    def setup_method(self):
        database.init_db()
        # テスト間隔離: 対象テーブルをクリア
        import sqlite3
        with sqlite3.connect(str(database.DB_PATH)) as conn:
            conn.execute("DELETE FROM blood_pressure_logs")
            conn.execute("DELETE FROM body_fat_logs")

    def test_get_blood_pressure_range_empty(self):
        result = database.get_blood_pressure_range("2026-04-01", "2026-04-07")
        assert isinstance(result, list)
        assert result == []

    def test_get_body_fat_range_empty(self):
        result = database.get_body_fat_range("2026-04-01", "2026-04-07")
        assert isinstance(result, list)
        assert result == []

    def test_get_blood_pressure_range_returns_dict_list(self):
        """戻り値がdict型リストであることを確認"""
        result = database.get_blood_pressure_range("2026-01-01", "2026-12-31")
        assert all(isinstance(r, dict) for r in result)

    def test_get_body_fat_range_returns_dict_list(self):
        """戻り値がdict型リストであることを確認"""
        result = database.get_body_fat_range("2026-01-01", "2026-12-31")
        assert all(isinstance(r, dict) for r in result)


# ---------------------------------------------------------------------------
# _build_comment_prompt
# ---------------------------------------------------------------------------
class TestBuildCommentPrompt:
    def _all_on(self):
        return [
            {"id": "meal_content", "enabled": True},
            {"id": "calories", "enabled": True},
            {"id": "pfc", "enabled": True},
            {"id": "weight", "enabled": True},
            {"id": "sodium", "enabled": True},
            {"id": "blood_pressure", "enabled": True},
            {"id": "body_fat", "enabled": True},
            {"id": "expenditure", "enabled": True},
            {"id": "exercise", "enabled": True},
            {"id": "steps", "enabled": True},
        ]

    def _all_off(self):
        items = self._all_on()
        for item in items:
            item["enabled"] = False
        return items

    def test_all_off_returns_dynamic_prompt(self):
        """全項目OFFの場合は動的プロンプトを返す（固定プロンプト廃止）"""
        result = _build_comment_prompt(self._all_off())
        assert "■ 前回比較" in result
        assert "300文字以内" in result

    def test_empty_list_returns_dynamic_prompt(self):
        """空リストの場合も動的プロンプトを返す（固定プロンプト廃止）"""
        result = _build_comment_prompt([])
        assert "■ 前回比較" in result
        assert "300文字以内" in result

    def test_all_on_contains_all_sections(self):
        """全項目ONの場合、全セクションがプロンプトに含まれる"""
        result = _build_comment_prompt(self._all_on())
        assert "前回比較" in result
        assert "パターン分析" in result
        assert "臨床的所見" in result

    def test_blood_pressure_on_includes_bp_section(self):
        """血圧ONの場合、血圧に関するセクションが含まれる"""
        items = [{"id": "blood_pressure", "enabled": True}]
        result = _build_comment_prompt(items)
        assert "血圧" in result

    def test_blood_pressure_off_excludes_bp_section(self):
        """血圧OFFの場合、血圧に関するセクションが含まれない"""
        items = [{"id": "blood_pressure", "enabled": False},
                 {"id": "weight", "enabled": True}]
        result = _build_comment_prompt(items)
        assert "血圧" not in result

    def test_body_fat_on_includes_section(self):
        """体脂肪率ONの場合、体脂肪率セクションが含まれる"""
        items = [{"id": "body_fat", "enabled": True}]
        result = _build_comment_prompt(items)
        assert "体脂肪率" in result

    def test_body_fat_off_excludes_section(self):
        """体脂肪率OFFの場合、体脂肪率セクションが含まれない"""
        items = [{"id": "body_fat", "enabled": False},
                 {"id": "weight", "enabled": True}]
        result = _build_comment_prompt(items)
        assert "体脂肪率" not in result

    def test_is_monthly_true_uses_monthly_label(self):
        """is_monthly=Trueの場合、月次の表現を使う"""
        result = _build_comment_prompt(self._all_on(), is_monthly=True)
        assert "1ヶ月" in result
        assert "月次" in result

    def test_is_monthly_false_uses_weekly_label(self):
        """is_monthly=Falseの場合（デフォルト）、週次の表現を使う"""
        result = _build_comment_prompt(self._all_on(), is_monthly=False)
        assert "1週間" in result
        assert "週次" in result

    def test_partial_on_only_includes_enabled_items(self):
        """一部ONの場合、ONの項目のみプロンプトに含まれる"""
        items = [
            {"id": "weight", "enabled": True},
            {"id": "calories", "enabled": False},
        ]
        result = _build_comment_prompt(items)
        assert "体重" in result
        # カロリー関連のセクションは含まれない
        assert "カロリー摂取" not in result

    def test_calories_on_includes_calorie_section(self):
        """カロリーONの場合、カロリーセクションが含まれる"""
        items = [{"id": "calories", "enabled": True}]
        result = _build_comment_prompt(items)
        assert "カロリー" in result

    def test_steps_on_includes_steps_section(self):
        """歩数ONの場合、歩数セクションが含まれる"""
        items = [{"id": "steps", "enabled": True}]
        result = _build_comment_prompt(items)
        assert "歩数" in result

    def test_char_limit_mentioned(self):
        """文字数制限の制約がプロンプトに含まれる（Phase25.2で 400→300 に変更）"""
        result = _build_comment_prompt(self._all_on())
        assert "300文字" in result


# ---------------------------------------------------------------------------
# generate_claude_comment — focus_items適用テスト
# ---------------------------------------------------------------------------
class TestGenerateClaudeCommentFocus:
    _base_data = {
        "start": "2026-04-07",
        "end": "2026-04-13",
        "days": [],
        "totals": {},
        "calorie_goal": 1800,
        "height_cm": 160,
        "weight_logs": [],
        "steps_logs": [],
        "meal_skip_days": [],
        "exercise_logs": [],
    }

    @pytest.mark.asyncio
    async def test_focus_none_uses_all_enabled(self):
        """focus_items=NoneのときすべてONとして動作（後方互換）"""
        mock_client = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="テストコメント")]
        mock_client.messages.create = AsyncMock(return_value=mock_msg)

        with patch("report_generator.anthropic.AsyncAnthropic", return_value=mock_client), \
             patch("report_generator.database.get_setting", return_value="test_key"), \
             patch("report_generator.database.get_blood_pressure_range", return_value=[]), \
             patch("report_generator.database.get_body_fat_range", return_value=[]), \
             patch("report_generator.database.get_exercise_logs", return_value=[]):
            result = await report_generator.generate_claude_comment(self._base_data, focus_items=None)
        assert result == "テストコメント"

    @pytest.mark.asyncio
    async def test_focus_blood_pressure_on_fetches_bp_data(self):
        """blood_pressure ONの場合、get_blood_pressure_range が呼ばれる"""
        focus_items = [{"id": "blood_pressure", "enabled": True}]
        mock_client = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="")]
        mock_client.messages.create = AsyncMock(return_value=mock_msg)

        mock_bp = MagicMock(return_value=[])
        mock_bf = MagicMock(return_value=[])
        mock_ex = MagicMock(return_value=[])

        with patch("report_generator.anthropic.AsyncAnthropic", return_value=mock_client), \
             patch("report_generator.database.get_setting", return_value="test_key"), \
             patch("report_generator.database.get_blood_pressure_range", mock_bp), \
             patch("report_generator.database.get_body_fat_range", mock_bf), \
             patch("report_generator.database.get_exercise_logs", mock_ex):
            await report_generator.generate_claude_comment(self._base_data, focus_items=focus_items)

        mock_bp.assert_called_once_with("2026-04-07", "2026-04-13")

    @pytest.mark.asyncio
    async def test_focus_blood_pressure_off_no_bp_fetch(self):
        """blood_pressure OFFの場合、get_blood_pressure_range が呼ばれない"""
        focus_items = [{"id": "blood_pressure", "enabled": False},
                       {"id": "weight", "enabled": True}]
        mock_client = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="")]
        mock_client.messages.create = AsyncMock(return_value=mock_msg)

        mock_bp = MagicMock(return_value=[])

        with patch("report_generator.anthropic.AsyncAnthropic", return_value=mock_client), \
             patch("report_generator.database.get_setting", return_value="test_key"), \
             patch("report_generator.database.get_blood_pressure_range", mock_bp), \
             patch("report_generator.database.get_body_fat_range", return_value=[]), \
             patch("report_generator.database.get_exercise_logs", return_value=[]):
            await report_generator.generate_claude_comment(self._base_data, focus_items=focus_items)

        mock_bp.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_api_key_returns_empty(self):
        """APIキー未設定の場合は空文字を返す"""
        focus_items = [{"id": "weight", "enabled": True}]
        with patch("report_generator.database.get_setting", return_value=None), \
             patch.dict(os.environ, {}, clear=True):
            if "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]
            result = await report_generator.generate_claude_comment(
                self._base_data, focus_items=focus_items
            )
        assert result == ""

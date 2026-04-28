"""Phase13-Step6: C-9 build_system_prompt リファクタテスト"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import database
import claude_client


def _make_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return patch.object(database, "DB_PATH", Path(tmp.name))


class TestBuildSystemPromptRefactor(unittest.TestCase):
    """build_system_prompt がリファクタ後も正しく動作することを検証"""

    def test_returns_two_blocks(self):
        """サマリーがある場合は2ブロック構成で返されること（Phase34変更: サマリーなし→1ブロック）"""
        with _make_db():
            database.init_db()
            # サマリーなし → 1ブロック
            result_no_summary = claude_client.build_system_prompt(summary=None)
            with patch.object(
                claude_client.database, "load_conversation_summary", return_value=None
            ):
                result_no_summary = claude_client.build_system_prompt()
            self.assertEqual(len(result_no_summary), 1)
            self.assertEqual(result_no_summary[0]["type"], "text")
            # サマリーあり → 2ブロック
            result_with_summary = claude_client.build_system_prompt(summary="テストサマリー")
            self.assertEqual(len(result_with_summary), 2)
            self.assertEqual(result_with_summary[0]["type"], "text")
            self.assertEqual(result_with_summary[1]["type"], "text")

    def test_block1_has_cache_control(self):
        """Block1にcache_controlが設定されていること"""
        with _make_db():
            database.init_db()
            result = claude_client.build_system_prompt()
            self.assertIn("cache_control", result[0])
            self.assertEqual(result[0]["cache_control"]["type"], "ephemeral")

    def test_block1_contains_user_info(self):
        """Block1にユーザー情報が含まれること"""
        with _make_db():
            database.init_db()
            database.save_setting("user_name", "テスト太郎")
            database.save_setting("user_height_cm", "175")
            database.save_setting("daily_calorie_goal", "2000")
            result = claude_client.build_system_prompt()
            block1 = result[0]["text"]
            self.assertIn("テスト太郎", block1)
            self.assertIn("175cm", block1)
            self.assertIn("2000kcal", block1)

    def test_block2_contains_summary(self):
        """サマリーがある場合 Block2 にサマリー内容が含まれること（Phase34変更: 日付はBlock2に含まれない）"""
        with _make_db():
            database.init_db()
            result = claude_client.build_system_prompt(summary="テスト用サマリー")
            block2 = result[1]["text"]
            self.assertIn("会話サマリー", block2)
            self.assertIn("テスト用サマリー", block2)

    def test_savings_mode(self):
        """節約モードで検索フローが変わること"""
        with _make_db():
            database.init_db()
            result_normal = claude_client.build_system_prompt(savings_mode=False)
            result_savings = claude_client.build_system_prompt(savings_mode=True)
            # 通常モード: 検索フロー手順（"1. search_food_nutrition で検索する"）
            self.assertIn("search_food_nutrition で検索する", result_normal[0]["text"])
            # 節約モード: 検索フローなし（"使用しない"の文脈で言及されるが手順は含まない）
            self.assertIn("節約モード", result_savings[0]["text"])
            self.assertNotIn("search_food_nutrition で検索する", result_savings[0]["text"])

    def test_split_items_enabled(self):
        """複数品目分割が有効の場合セクションが含まれること"""
        with _make_db():
            database.init_db()
            database.save_setting("split_multiple_items", "true")
            result = claude_client.build_system_prompt()
            self.assertIn("複数品目の個別記録", result[0]["text"])

    def test_split_items_disabled(self):
        """複数品目分割が無効の場合セクションが含まれないこと"""
        with _make_db():
            database.init_db()
            database.save_setting("split_multiple_items", "false")
            result = claude_client.build_system_prompt()
            self.assertNotIn("複数品目の個別記録", result[0]["text"])

    def test_auto_save_enabled(self):
        """food_defaults自動保存が有効の場合セクションが含まれること"""
        with _make_db():
            database.init_db()
            database.save_setting("auto_save_food_defaults", "true")
            result = claude_client.build_system_prompt()
            self.assertIn("food_defaults自動保存", result[0]["text"])

    def test_auto_save_disabled(self):
        """food_defaults自動保存が無効の場合セクションが含まれないこと"""
        with _make_db():
            database.init_db()
            database.save_setting("auto_save_food_defaults", "false")
            result = claude_client.build_system_prompt()
            self.assertNotIn("food_defaults自動保存", result[0]["text"])

    def test_user_notes_included(self):
        """注意事項がBlock1に含まれること"""
        with _make_db():
            database.init_db()
            database.save_setting("user_notes", "塩分制限6g/日")
            result = claude_client.build_system_prompt()
            self.assertIn("塩分制限6g/日", result[0]["text"])

    def test_summary_in_block2(self):
        """会話サマリーがBlock2に含まれること"""
        with _make_db():
            database.init_db()
            result = claude_client.build_system_prompt(summary="テストサマリー")
            self.assertIn("テストサマリー", result[1]["text"])

    def test_cache_ttl_1hour(self):
        """cache_ttl=1hourでTTL設定が変わること"""
        with _make_db():
            database.init_db()
            database.save_setting("cache_ttl", "1hour")
            result = claude_client.build_system_prompt()
            self.assertEqual(result[0]["cache_control"]["ttl"], "1h")

    def test_essential_rules_present(self):
        """記録ルール・量の換算ルール等の必須セクションが含まれること"""
        with _make_db():
            database.init_db()
            block1 = claude_client.build_system_prompt()[0]["text"]
            self.assertIn("記録ルール", block1)
            self.assertIn("量の換算ルール", block1)
            self.assertIn("食事提案", block1)
            self.assertIn("確認メッセージの形式", block1)
            self.assertIn("record_meal_skip", block1)


class TestHelperFunctions(unittest.TestCase):
    """ヘルパー関数の単体テスト"""

    def test_build_search_flow_savings(self):
        result = claude_client._build_search_flow_section(True)
        self.assertIn("節約モード", result)

    def test_build_search_flow_normal(self):
        result = claude_client._build_search_flow_section(False)
        self.assertIn("search_food_nutrition", result)

    def test_build_auto_save_enabled(self):
        result = claude_client._build_auto_save_section(True)
        self.assertIn("food_defaults自動保存", result)

    def test_build_auto_save_disabled(self):
        result = claude_client._build_auto_save_section(False)
        self.assertEqual(result, "")

    def test_build_split_enabled(self):
        result = claude_client._build_split_section(True)
        self.assertIn("複数品目の個別記録", result)

    def test_build_split_disabled(self):
        result = claude_client._build_split_section(False)
        self.assertEqual(result, "")


class TestFunctionLineCount(unittest.TestCase):
    """build_system_prompt が100行以下であることを検証"""

    def test_under_100_lines(self):
        """build_system_promptが100行以下"""
        import inspect
        source = inspect.getsource(claude_client.build_system_prompt)
        line_count = len(source.strip().splitlines())
        self.assertLessEqual(line_count, 100, f"build_system_prompt is {line_count} lines (max 100)")


if __name__ == "__main__":
    unittest.main()

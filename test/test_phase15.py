"""
Phase 15: 日の区切り設定 & AI サマリーバグ修正 テスト（17件）

検証対象:
- database.get_logical_today_jst()        : 日の区切り考慮の論理日付
- database.get_daily_summary()            : デフォルト日付が論理日付を使う
- main._classify_weight_record()          : day_start_hour パラメータ対応
- claude_client.build_system_prompt()     : 論理日付・day_start_hour・get_daily_summary ルール
- database.get_setting("day_start_hour")  : デフォルト値 "4"
- GET /api/settings                       : day_start_hour フィールドを含む
- POST /api/settings                      : day_start_hour の保存・バリデーション

テスト方針（CLAUDE.md規約）:
- DB操作: tempfile DB + monkeypatch.setattr(database, "DB_PATH")
- 時刻依存: unittest.mock.patch('database.datetime') で datetime.now をモック
- API: TestClient + logged_in_client フィクスチャ
- build_system_prompt: 動的埋め込み値（日付・day_start_hour・ツール名）を検証
- teardown: gc.collect() で Windows WALロックを解放

実行: uv run pytest test/test_phase15.py -v
"""
import gc
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import claude_client
import database
import main
from main import _classify_weight_record, app

JST = timezone(timedelta(hours=9))


# ── フィクスチャ ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    """一時DBを作成して database.DB_PATH をパッチ。Windows WALロック対策付き。"""
    db_file = tmp_path / "test_phase15.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    database.init_db()
    yield db_file
    gc.collect()


@pytest.fixture()
def logged_in_client(tmp_db):
    """ログイン済みの TestClient を返す。"""
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post("/api/login", json={"password": "1234"})
    assert res.status_code == 200
    return client


# ── #1-4: get_logical_today_jst() ────────────────────────────────────────────

class TestGetLogicalTodayJst:
    """day_start_hour 設定に基づく論理日付の計算を検証する。
    datetime.now を patch して時刻を固定することで時刻依存を排除する。"""

    def test_before_boundary_returns_prev_day(self, tmp_db):
        """day_start_hour=4, 現在時刻 3:59 → 前日を返す"""
        database.save_setting("day_start_hour", "4")
        fixed = datetime(2026, 4, 1, 3, 59, tzinfo=JST)
        with patch("database.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            result = database.get_logical_today_jst()
        assert result == "2026-03-31"

    def test_at_boundary_returns_today(self, tmp_db):
        """day_start_hour=4, 現在時刻 4:00 → 今日を返す"""
        database.save_setting("day_start_hour", "4")
        fixed = datetime(2026, 4, 1, 4, 0, tzinfo=JST)
        with patch("database.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            result = database.get_logical_today_jst()
        assert result == "2026-04-01"

    def test_boundary_zero_always_returns_today(self, tmp_db):
        """day_start_hour=0 → 0時台も今日を返す（0時以前は存在しない）"""
        database.save_setting("day_start_hour", "0")
        fixed = datetime(2026, 4, 1, 0, 30, tzinfo=JST)
        with patch("database.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            result = database.get_logical_today_jst()
        assert result == "2026-04-01"

    def test_high_boundary_returns_prev_day(self, tmp_db):
        """day_start_hour=23, 現在時刻 22:30 → 前日を返す"""
        database.save_setting("day_start_hour", "23")
        fixed = datetime(2026, 4, 1, 22, 30, tzinfo=JST)
        with patch("database.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            result = database.get_logical_today_jst()
        assert result == "2026-03-31"


# ── #5: get_daily_summary() デフォルト日付 ──────────────────────────────────

class TestGetDailySummaryLogicalDate:
    """get_daily_summary() 引数なし呼び出しが論理日付を使うことを検証する。"""

    def test_default_date_uses_logical_today(self, tmp_db):
        """引数なし呼び出し → get_logical_today_jst() と同じ日付を返す。
        day_start_hour=4 かつ時刻 3:00 → 前日 2026-03-31 を返すこと。"""
        database.save_setting("day_start_hour", "4")
        fixed = datetime(2026, 4, 1, 3, 0, tzinfo=JST)
        with patch("database.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            summary = database.get_daily_summary()
        assert summary["date"] == "2026-03-31"


# ── #6-9: _classify_weight_record() day_start_hour パラメータ ────────────────

class TestClassifyWeightRecordParameterized:
    """day_start_hour パラメータを明示して分類ロジックを検証する。
    純粋関数のためDB・モック不要。"""

    def test_before_boundary_is_prev_evening(self):
        """day_start_hour=4, 3:59 → 前日 evening（境界直前）"""
        log_date, tod = _classify_weight_record("2026/04/01 03:59", day_start_hour=4)
        assert log_date == "2026-03-31"
        assert tod == "evening"

    def test_at_boundary_is_morning(self):
        """day_start_hour=4, 4:00 → 当日 morning（境界）"""
        log_date, tod = _classify_weight_record("2026/04/01 04:00", day_start_hour=4)
        assert log_date == "2026-04-01"
        assert tod == "morning"

    def test_custom_boundary_reflects_setting(self):
        """day_start_hour=6, 5:30 → 前日 evening（設定値が反映される）"""
        log_date, tod = _classify_weight_record("2026/04/01 05:30", day_start_hour=6)
        assert log_date == "2026-03-31"
        assert tod == "evening"

    def test_zero_boundary_midnight_is_morning(self):
        """day_start_hour=0, 0:30 → 当日 morning（0時以前は存在しないため常に今日）"""
        log_date, tod = _classify_weight_record("2026/04/01 00:30", day_start_hour=0)
        assert log_date == "2026-04-01"
        assert tod == "morning"


# ── #10-12: build_system_prompt() ────────────────────────────────────────────

class TestBuildSystemPrompt:
    """build_system_prompt() の動的埋め込みを検証する。
    特定の日本語テキストではなく動的値・ツール名の埋め込みを確認する。"""

    def test_block2_not_present_without_summary(self, tmp_db):
        """サマリーがない場合 Block 2 が省略されること（Phase34変更: 日付はBlock2から削除）"""
        blocks = claude_client.build_system_prompt(summary=None)
        # サマリーなし → 1ブロックのみ
        assert len(blocks) == 1

    def test_block1_references_get_daily_summary_tool(self, tmp_db):
        """Block 1 テキストに "get_daily_summary" が含まれる（ツール名の参照確認）"""
        blocks = claude_client.build_system_prompt()
        block1_text = blocks[0]["text"]
        assert "get_daily_summary" in block1_text


# ── #13: デフォルト設定値 ────────────────────────────────────────────────────

class TestDefaultSettings:
    def test_day_start_hour_default_is_4(self, tmp_db):
        """init_db() 後に day_start_hour のデフォルト値 "4" が返る"""
        assert database.get_setting("day_start_hour") == "4"


# ── #14-17: /api/settings バリデーション ────────────────────────────────────

class TestSettingsApi:
    def test_get_settings_includes_day_start_hour(self, logged_in_client):
        """GET /api/settings のレスポンスに day_start_hour フィールドが含まれる"""
        res = logged_in_client.get("/api/settings")
        assert res.status_code == 200
        data = res.json()
        assert "day_start_hour" in data

    def test_save_and_retrieve_day_start_hour(self, logged_in_client, tmp_db):
        """POST /api/settings で day_start_hour: "6" を保存後、GET で "6" が取得できる"""
        res = logged_in_client.post(
            "/api/settings", json={"settings": {"day_start_hour": "6"}}
        )
        assert res.status_code == 200
        res2 = logged_in_client.get("/api/settings")
        assert res2.json()["day_start_hour"] == "6"

    def test_day_start_hour_24_returns_422(self, logged_in_client):
        """day_start_hour: "24" は 422 エラー（範囲 0〜23 を超過）"""
        res = logged_in_client.post(
            "/api/settings", json={"settings": {"day_start_hour": "24"}}
        )
        assert res.status_code == 422

    def test_day_start_hour_minus1_returns_422(self, logged_in_client):
        """day_start_hour: "-1" は 422 エラー（負の値は範囲外）"""
        res = logged_in_client.post(
            "/api/settings", json={"settings": {"day_start_hour": "-1"}}
        )
        assert res.status_code == 422

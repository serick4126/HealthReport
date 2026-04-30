# test/test_weather_db.py
"""weather_logs テーブルと設定の統合テスト"""
import pytest
from database import (
    init_db,
    get_setting,
    get_weather_log,
    upsert_weather_log,
    get_weather_logs_range,
)


@pytest.fixture(autouse=True)
def _init_test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_weather.db"
    monkeypatch.setattr("database.DB_PATH", str(db_path))
    init_db()
    # 天気関連のデフォルト設定が存在することを確認
    assert get_setting("prefecture_code") == "JP.13"
    assert get_setting("country_code") == "JP"
    assert get_setting("weather_api_enabled") == "false"


class TestWeatherLogsTable:
    """weather_logs テーブルの CRUD テスト"""

    def test_insert_and_get(self):
        """INSERT → GET が正常に動作すること"""
        upsert_weather_log("2026-04-15", "晴れのちくもり", "2026-04-16 09:00:00")
        row = get_weather_log("2026-04-15")
        assert row is not None
        assert row["weather"] == "晴れのちくもり"
        assert row["recorded_at"] == "2026-04-16 09:00:00"

    def test_upsert_overwrites(self):
        """同一日付の upsert が上書きされること"""
        upsert_weather_log("2026-04-16", "晴れ", "2026-04-17 08:00:00")
        upsert_weather_log("2026-04-16", "雨", "2026-04-17 10:00:00")
        row = get_weather_log("2026-04-16")
        assert row["weather"] == "雨"
        assert row["recorded_at"] == "2026-04-17 10:00:00"

    def test_get_nonexistent(self):
        """存在しない日付は None"""
        assert get_weather_log("2099-01-01") is None

    def test_get_range(self):
        """期間取得で日付順に返ること"""
        upsert_weather_log("2026-04-10", "晴れ", "2026-04-11 08:00:00")
        upsert_weather_log("2026-04-12", "くもり", "2026-04-13 08:00:00")
        upsert_weather_log("2026-04-14", "雨", "2026-04-15 08:00:00")

        rows = get_weather_logs_range("2026-04-10", "2026-04-14")
        assert len(rows) == 3
        assert rows[0]["weather"] == "晴れ"
        assert rows[1]["weather"] == "くもり"
        assert rows[2]["weather"] == "雨"

    def test_get_range_partial(self):
        """一部のみデータがある期間"""
        upsert_weather_log("2026-04-15", "晴れ", "2026-04-16 08:00:00")
        rows = get_weather_logs_range("2026-04-10", "2026-04-20")
        assert len(rows) == 1

    def test_get_range_empty(self):
        """データなし期間は空リスト"""
        rows = get_weather_logs_range("2099-01-01", "2099-01-07")
        assert rows == []


class TestWeatherSettingsDefaults:
    """天気関連 app_settings のデフォルト値"""

    def test_default_prefecture_code(self):
        assert get_setting("prefecture_code") == "JP.13"

    def test_default_country_code(self):
        assert get_setting("country_code") == "JP"

    def test_default_weather_api_enabled(self):
        assert get_setting("weather_api_enabled") == "false"

    def test_default_latitude(self):
        assert get_setting("latitude") == "35.6895"

    def test_default_longitude(self):
        assert get_setting("longitude") == "139.6917"

    def test_default_timezone(self):
        assert get_setting("timezone") == "Asia/Tokyo"

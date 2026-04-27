"""Phase30: 週次レポート週開始曜日設定テスト"""
import os
import tempfile
import pytest
from fastapi.testclient import TestClient

import database


@pytest.fixture
def temp_db(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db()
    yield db_path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except OSError:
            pass


@pytest.fixture
def client(temp_db):
    import main
    c = TestClient(main.app, cookies={"session_id": "test-session"})
    main.sessions.add("test-session")
    yield c
    main.sessions.discard("test-session")


def _insert_steps(db_path, date_str, steps=5000):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO steps_logs (log_date, steps) VALUES (?, ?)",
        (date_str, steps),
    )
    conn.commit()
    conn.close()


class TestGetReportWeeksSunday:
    """week_start_day='sunday' の週区切り検証"""

    def test_friday_belongs_to_sun_sat_week(self, temp_db):
        """2026-04-10（金）は日曜始まりで 2026-04-05〜2026-04-11 の週に属する"""
        _insert_steps(temp_db, "2026-04-10")
        result = database.get_report_weeks(week_start_day="sunday")
        starts = [w["start"] for w in result]
        ends = [w["end"] for w in result]
        assert "2026-04-05" in starts
        assert "2026-04-11" in ends

    def test_sunday_is_week_start(self, temp_db):
        """2026-04-05（日）は日曜始まりでその日自身が週初め"""
        _insert_steps(temp_db, "2026-04-05")
        result = database.get_report_weeks(week_start_day="sunday")
        starts = [w["start"] for w in result]
        assert "2026-04-05" in starts

    def test_saturday_is_week_end(self, temp_db):
        """2026-04-11（土）は日曜始まりで 2026-04-05〜2026-04-11 の末日"""
        _insert_steps(temp_db, "2026-04-11")
        result = database.get_report_weeks(week_start_day="sunday")
        ends = [w["end"] for w in result]
        assert "2026-04-11" in ends

    def test_default_is_sunday(self, temp_db):
        """引数省略時は sunday と同じ結果を返す"""
        _insert_steps(temp_db, "2026-04-10")
        result_default = database.get_report_weeks()
        result_sunday = database.get_report_weeks(week_start_day="sunday")
        assert result_default == result_sunday


class TestGetReportWeeksMonday:
    """week_start_day='monday' の週区切り検証"""

    def test_friday_belongs_to_mon_sun_week(self, temp_db):
        """2026-04-10（金）は月曜始まりで 2026-04-06〜2026-04-12 の週に属する"""
        _insert_steps(temp_db, "2026-04-10")
        result = database.get_report_weeks(week_start_day="monday")
        starts = [w["start"] for w in result]
        ends = [w["end"] for w in result]
        assert "2026-04-06" in starts
        assert "2026-04-12" in ends

    def test_monday_is_week_start(self, temp_db):
        """2026-04-06（月）は月曜始まりでその日自身が週初め"""
        _insert_steps(temp_db, "2026-04-06")
        result = database.get_report_weeks(week_start_day="monday")
        starts = [w["start"] for w in result]
        assert "2026-04-06" in starts

    def test_sunday_is_week_end(self, temp_db):
        """2026-04-12（日）は月曜始まりで 2026-04-06〜2026-04-12 の末日"""
        _insert_steps(temp_db, "2026-04-12")
        result = database.get_report_weeks(week_start_day="monday")
        ends = [w["end"] for w in result]
        assert "2026-04-12" in ends

    def test_monday_and_sunday_same_week(self, temp_db):
        """同じ週（月〜日）に属するデータは1件の週エントリにまとまる"""
        _insert_steps(temp_db, "2026-04-06", steps=5000)  # 月
        _insert_steps(temp_db, "2026-04-12", steps=6000)  # 日
        result = database.get_report_weeks(week_start_day="monday")
        assert len(result) == 1
        assert result[0]["start"] == "2026-04-06"
        assert result[0]["end"] == "2026-04-12"

    def test_two_weeks_monday_pattern(self, temp_db):
        """2週にまたがるデータは2件の週エントリを降順で返す"""
        _insert_steps(temp_db, "2026-04-06")   # 週1
        _insert_steps(temp_db, "2026-04-13")   # 週2
        result = database.get_report_weeks(week_start_day="monday")
        assert len(result) == 2
        starts = [w["start"] for w in result]
        assert starts == sorted(starts, reverse=True)

    def test_sunday_in_next_week_not_previous(self, temp_db):
        """2026-04-12（日）は月曜始まりでは 2026-04-06 週に属し 2026-03-30 週ではない"""
        _insert_steps(temp_db, "2026-04-12")
        result = database.get_report_weeks(week_start_day="monday")
        starts = [w["start"] for w in result]
        assert "2026-03-30" not in starts
        assert "2026-04-06" in starts


class TestDefaultSettingInDB:
    """DBに保存される report_week_start_day デフォルト値の検証"""

    def test_default_value_is_sunday(self, temp_db):
        """init_db() 後、report_week_start_day のデフォルト値は 'sunday'"""
        value = database.get_setting("report_week_start_day")
        assert value == "sunday"

    def test_invalid_report_week_start_day_returns_400(self, client):
        """POST /api/settings に不正な report_week_start_day を送信すると 400 エラーを返す"""
        res = client.post("/api/settings",
                          json={"settings": {"report_week_start_day": "wednesday"}})
        assert res.status_code == 400

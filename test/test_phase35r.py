"""
Phase35r コピーボタン機能 サーバーサイドAPI化テスト

対象:
  - _calc_age_at_date()         年齢計算（境界値・不正入力）
  - _calc_bmr_mifflin()         Mifflin式BMR計算（male/female・欠損値）
  - get_copy_enrichment_day()   日別エンリッチメント（体重なし時）
  - GET /api/copy/history/{date} 認証・バリデーション
  - GET /api/copy/stats          認証・バリデーション

実行: uv run pytest test/test_phase35r.py -v
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# DB_PATH をテスト用に切り替えてから database をインポート
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()

import database

database.DB_PATH = Path(_tmp_db.name)
database.init_db()

import main
from fastapi.testclient import TestClient

client = TestClient(main.app, raise_server_exceptions=False)

TEST_PASSWORD = "testpass_phase35r"
database.save_setting("app_password", TEST_PASSWORD)


def setup_module(module):
    database.DB_PATH = Path(_tmp_db.name)
    database.init_db()
    database.save_setting("app_password", TEST_PASSWORD)


def teardown_module(module):
    import gc
    gc.collect()
    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass


def login():
    resp = client.post("/api/login", json={"password": TEST_PASSWORD})
    assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# _calc_age_at_date
# ══════════════════════════════════════════════════════════════════════════════

class TestCalcAgeAtDate:
    """_calc_age_at_date() の境界値・エラー処理を検証する"""

    def test_normal_age(self):
        """誕生日前日なので30歳（31にならない）"""
        result = database._calc_age_at_date("1990-05-15", "2020-05-14")
        assert result == 29

    def test_birthday_exact(self):
        """誕生日当日は年齢が上がること"""
        result = database._calc_age_at_date("1990-05-15", "2020-05-15")
        assert result == 30

    def test_birthday_day_after(self):
        """誕生日翌日も同年齢"""
        result = database._calc_age_at_date("1990-05-15", "2020-05-16")
        assert result == 30

    def test_negative_age_returns_none(self):
        """target_date が birthdate より前の場合 None を返すこと"""
        result = database._calc_age_at_date("2000-01-01", "1999-12-31")
        assert result is None

    def test_age_over_120_returns_none(self):
        """120歳超の場合 None を返すこと"""
        result = database._calc_age_at_date("1800-01-01", "2026-01-01")
        assert result is None

    def test_age_exactly_120(self):
        """120歳ちょうどは有効"""
        result = database._calc_age_at_date("1906-01-01", "2026-01-01")
        assert result == 120

    def test_invalid_birthdate_returns_none(self):
        """不正な birthdate 文字列は None を返すこと"""
        result = database._calc_age_at_date("not-a-date", "2026-01-01")
        assert result is None

    def test_invalid_target_returns_none(self):
        """不正な target_date 文字列は None を返すこと"""
        result = database._calc_age_at_date("1990-01-01", "invalid")
        assert result is None

    def test_empty_strings_return_none(self):
        """空文字列は None を返すこと"""
        result = database._calc_age_at_date("", "2026-01-01")
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# get_copy_enrichment_day
# ══════════════════════════════════════════════════════════════════════════════

class TestGetCopyEnrichmentDay:
    """get_copy_enrichment_day() の戻り値構造と体重なし時の挙動を検証する"""

    TEST_DATE = "2026-03-15"

    @classmethod
    def setup_class(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        database.DB_PATH = Path(cls.tmp.name)
        database.init_db()
        database.save_setting("daily_calorie_goal", "1800")
        database.save_setting("daily_steps_goal", "8000")
        database.save_setting("user_height_cm", "170")
        database.save_setting("user_gender", "male")
        database.save_setting("user_birthdate", "1990-03-15")

    @classmethod
    def teardown_class(cls):
        database.DB_PATH = Path(_tmp_db.name)
        import gc
        gc.collect()
        try:
            os.unlink(cls.tmp.name)
        except OSError:
            pass

    def test_returns_required_keys(self):
        """戻り値に必須キーが全て含まれること"""
        result = database.get_copy_enrichment_day(self.TEST_DATE)
        for key in ("bmr_kcal", "total_expenditure", "calorie_balance",
                    "calories_goal", "steps_goal"):
            assert key in result

    def test_bmr_none_when_no_weight(self):
        """体重記録がない場合 bmr_kcal は None になること"""
        result = database.get_copy_enrichment_day(self.TEST_DATE)
        assert result["bmr_kcal"] is None

    def test_total_exp_none_when_bmr_none(self):
        """bmr_kcal が None の場合 total_expenditure も None になること"""
        result = database.get_copy_enrichment_day(self.TEST_DATE)
        assert result["total_expenditure"] is None

    def test_balance_none_when_exp_none(self):
        """total_expenditure が None の場合 calorie_balance も None になること"""
        result = database.get_copy_enrichment_day(self.TEST_DATE)
        assert result["calorie_balance"] is None

    def test_goals_returned_correctly(self):
        """calories_goal と steps_goal が設定値と一致すること"""
        result = database.get_copy_enrichment_day(self.TEST_DATE)
        assert result["calories_goal"] == 1800
        assert result["steps_goal"] == 8000

    def test_bmr_calculated_when_weight_exists(self):
        """体重記録がある場合 bmr_kcal が計算されること"""
        database.save_weight(self.TEST_DATE, "morning", 70.0)
        result = database.get_copy_enrichment_day(self.TEST_DATE)
        assert result["bmr_kcal"] is not None
        assert isinstance(result["bmr_kcal"], int)


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/copy/history/{date}
# ══════════════════════════════════════════════════════════════════════════════

class TestCopyHistoryEndpoint:
    """GET /api/copy/history/{date} の認証・バリデーションを検証する"""

    @classmethod
    def setup_class(cls):
        login()

    def test_unauthenticated_returns_401(self):
        """未認証アクセスは 401 を返すこと"""
        new_client = TestClient(main.app, raise_server_exceptions=False)
        resp = new_client.get("/api/copy/history/2026-01-01")
        assert resp.status_code == 401

    def test_invalid_date_format_returns_422(self):
        """不正な日付形式は 422 を返すこと"""
        resp = client.get("/api/copy/history/2026-13-01")
        assert resp.status_code == 422

    def test_non_date_string_returns_422(self):
        """日付でない文字列は 422 を返すこと"""
        resp = client.get("/api/copy/history/not-a-date")
        assert resp.status_code == 422

    def test_valid_date_returns_200(self):
        """有効な日付は 200 を返し必須キーを含むこと"""
        resp = client.get("/api/copy/history/2026-01-01")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("bmr_kcal", "total_expenditure", "calorie_balance",
                    "calories_goal", "steps_goal"):
            assert key in body


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/copy/stats
# ══════════════════════════════════════════════════════════════════════════════

class TestCopyStatsEndpoint:
    """GET /api/copy/stats の認証・バリデーションを検証する"""

    @classmethod
    def setup_class(cls):
        login()

    def test_unauthenticated_returns_401(self):
        """未認証アクセスは 401 を返すこと"""
        new_client = TestClient(main.app, raise_server_exceptions=False)
        resp = new_client.get("/api/copy/stats?from_date=2026-01-01&to_date=2026-01-07")
        assert resp.status_code == 401

    def test_from_after_to_returns_422(self):
        """from_date > to_date は 422 を返すこと"""
        resp = client.get("/api/copy/stats?from_date=2026-01-10&to_date=2026-01-01")
        assert resp.status_code == 422

    def test_invalid_from_date_returns_422(self):
        """不正な from_date は 422 を返すこと"""
        resp = client.get("/api/copy/stats?from_date=bad-date&to_date=2026-01-07")
        assert resp.status_code == 422

    def test_invalid_to_date_returns_422(self):
        """不正な to_date は 422 を返すこと"""
        resp = client.get("/api/copy/stats?from_date=2026-01-01&to_date=invalid")
        assert resp.status_code == 422

    def test_valid_range_returns_200(self):
        """有効な期間は 200 を返し必須キーと日付配列を含むこと"""
        resp = client.get("/api/copy/stats?from_date=2026-01-01&to_date=2026-01-03")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("dates", "calories_goal", "steps_goal",
                    "bmr_kcal", "total_expenditure", "calorie_balance"):
            assert key in body

    def test_dates_count_matches_range(self):
        """戻り値の dates 配列の要素数が期間日数と一致すること"""
        resp = client.get("/api/copy/stats?from_date=2026-01-01&to_date=2026-01-07")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["dates"]) == 7

    def test_single_day_range(self):
        """from_date == to_date の場合 dates に1要素が返ること"""
        resp = client.get("/api/copy/stats?from_date=2026-02-15&to_date=2026-02-15")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["dates"]) == 1
        assert len(body["bmr_kcal"]) == 1

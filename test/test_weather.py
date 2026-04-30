# test/test_weather.py
"""weather.py のユニットテスト"""
import pytest
from weather import (
    PREFECTURE_MASTER,
    weathercode_to_category,
    build_weather_string,
    HOUR_INDICES,
    get_coords_for_prefecture,
    validate_date_range,
    WEATHER_CATEGORIES,
)


class TestPrefectureMaster:
    """都道府県マスタの検証"""

    def test_has_47_prefectures(self):
        """47都道府県すべてが登録されていること"""
        assert len(PREFECTURE_MASTER) == 47

    def test_all_keys_start_with_JP(self):
        """すべてのキーが JP.XX 形式であること"""
        for key in PREFECTURE_MASTER:
            assert key.startswith("JP."), f"{key} が JP. 形式でない"

    def test_all_have_required_fields(self):
        """全エントリに name, lat, lng, tz が存在すること"""
        for key, entry in PREFECTURE_MASTER.items():
            assert "name" in entry, f"{key} に name がない"
            assert "lat" in entry, f"{key} に lat がない"
            assert "lng" in entry, f"{key} に lng がない"
            assert "tz" in entry, f"{key} に tz がない"

    def test_default_tokyo(self):
        """JP.13 が東京都であること"""
        assert PREFECTURE_MASTER["JP.13"]["name"] == "東京都"
        assert PREFECTURE_MASTER["JP.13"]["lat"] == 35.6895
        assert PREFECTURE_MASTER["JP.13"]["lng"] == 139.6917


class TestWeathercodeToCategory:
    """weathercode → カテゴリ変換の検証"""

    def test_sunny_codes(self):
        """0〜1 が '晴れ' になること"""
        assert weathercode_to_category(0) == "晴れ"
        assert weathercode_to_category(1) == "晴れ"

    def test_cloudy_codes(self):
        """2〜3, 45, 48 が 'くもり' になること"""
        for code in [2, 3, 45, 48]:
            assert weathercode_to_category(code) == "くもり"

    def test_rain_codes(self):
        """51〜57, 61〜67, 80〜82, 95〜99 が '雨' になること"""
        for code in [51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 97, 98, 99]:
            assert weathercode_to_category(code) == "雨", f"weathercode {code} が雨でない"

    def test_snow_codes(self):
        """71〜77, 85〜86 が '雪' になること"""
        for code in [71, 73, 75, 76, 77, 85, 86]:
            assert weathercode_to_category(code) == "雪", f"weathercode {code} が雪でない"

    def test_unknown_code_fallback(self):
        """想定外のコードは 'くもり' にフォールバックすること"""
        assert weathercode_to_category(100) == "くもり"
        assert weathercode_to_category(-1) == "くもり"
        assert weathercode_to_category(999) == "くもり"


class TestBuildWeatherString:
    """3時点のカテゴリ → 天気文字列の変換"""

    def test_all_same(self):
        """A == B == C → 'A'"""
        assert build_weather_string("晴れ", "晴れ", "晴れ") == "晴れ"

    def test_a_and_b_same_c_diff(self):
        """A == B != C → 'AのちC'"""
        assert build_weather_string("晴れ", "晴れ", "くもり") == "晴れのちくもり"

    def test_b_and_c_same_a_diff(self):
        """A != B == C → 'AのちB'"""
        assert build_weather_string("晴れ", "くもり", "くもり") == "晴れのちくもり"

    def test_a_and_c_same_b_diff(self):
        """A == C != B → 'A一時B'"""
        assert build_weather_string("晴れ", "くもり", "晴れ") == "晴れ一時くもり"

    def test_all_different(self):
        """A != B かつ B != C → 'AのちBのちC'"""
        assert build_weather_string("晴れ", "くもり", "雨") == "晴れのちくもりのち雨"

    def test_adjacent_duplicate_different(self):
        """A == B だが C が違う場合 → 'AのちC'"""
        assert build_weather_string("雨", "雨", "雪") == "雨のち雪"


class TestGetCoordsForPrefecture:
    """prefecture_code → 座標導出"""

    def test_valid_code(self):
        """有効なコードで座標が返ること"""
        lat, lng, tz = get_coords_for_prefecture("JP.13")
        assert lat == 35.6895
        assert lng == 139.6917
        assert tz == "Asia/Tokyo"

    def test_unknown_code_raises(self):
        """不明なコードで ValueError が発生すること"""
        with pytest.raises(ValueError, match="不明な都道府県コード"):
            get_coords_for_prefecture("JP.99")


class TestHOUR_INDICES:
    """気温判定用の時間インデックス"""

    def test_hour_indices(self):
        """6, 12, 18 時のインデックスが正しいこと"""
        assert HOUR_INDICES == (6, 12, 18)


class TestValidateDateRange:
    """日付範囲バリデーション"""

    def test_past_date_valid(self):
        """昨日は有効"""
        import datetime as _dt
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        assert validate_date_range(yesterday, yesterday) == True

    def test_today_invalid(self):
        """当日は常に無効"""
        import datetime as _dt
        today = _dt.date.today().isoformat()
        assert validate_date_range(today, today) == False

    def test_too_old_invalid(self):
        """367日前は無効"""
        import datetime as _dt
        too_old = (_dt.date.today() - _dt.timedelta(days=367)).isoformat()
        assert validate_date_range(too_old, too_old) == False

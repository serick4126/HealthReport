# weather.py
"""天気データ：Open-Meteo API取得・都道府県マスタ・天気コード変換"""
from __future__ import annotations
import logging
from datetime import date, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── 都道府県マスタ ───────────────────────────────────────────────────────────
# GeoNames admin1CodesASCII.txt 準拠（JP.XX 形式）。座標は庁所在地の代表値。

PREFECTURE_MASTER = {
    "JP.01": {"name": "北海道",   "lat": 43.0642, "lng": 141.3469, "tz": "Asia/Tokyo"},
    "JP.02": {"name": "青森県",   "lat": 40.8244, "lng": 140.7400, "tz": "Asia/Tokyo"},
    "JP.03": {"name": "岩手県",   "lat": 39.7036, "lng": 141.1527, "tz": "Asia/Tokyo"},
    "JP.04": {"name": "宮城県",   "lat": 38.2688, "lng": 140.8721, "tz": "Asia/Tokyo"},
    "JP.05": {"name": "秋田県",   "lat": 39.7186, "lng": 140.1024, "tz": "Asia/Tokyo"},
    "JP.06": {"name": "山形県",   "lat": 38.2404, "lng": 140.3636, "tz": "Asia/Tokyo"},
    "JP.07": {"name": "福島県",   "lat": 37.7500, "lng": 140.4676, "tz": "Asia/Tokyo"},
    "JP.08": {"name": "茨城県",   "lat": 36.3418, "lng": 140.4468, "tz": "Asia/Tokyo"},
    "JP.09": {"name": "栃木県",   "lat": 36.5657, "lng": 139.8836, "tz": "Asia/Tokyo"},
    "JP.10": {"name": "群馬県",   "lat": 36.3911, "lng": 139.0608, "tz": "Asia/Tokyo"},
    "JP.11": {"name": "埼玉県",   "lat": 35.8570, "lng": 139.6489, "tz": "Asia/Tokyo"},
    "JP.12": {"name": "千葉県",   "lat": 35.6073, "lng": 140.1063, "tz": "Asia/Tokyo"},
    "JP.13": {"name": "東京都",   "lat": 35.6895, "lng": 139.6917, "tz": "Asia/Tokyo"},
    "JP.14": {"name": "神奈川県", "lat": 35.4478, "lng": 139.6425, "tz": "Asia/Tokyo"},
    "JP.15": {"name": "新潟県",   "lat": 37.9026, "lng": 139.0232, "tz": "Asia/Tokyo"},
    "JP.16": {"name": "富山県",   "lat": 36.6953, "lng": 137.2113, "tz": "Asia/Tokyo"},
    "JP.17": {"name": "石川県",   "lat": 36.5947, "lng": 136.6256, "tz": "Asia/Tokyo"},
    "JP.18": {"name": "福井県",   "lat": 36.0652, "lng": 136.2217, "tz": "Asia/Tokyo"},
    "JP.19": {"name": "山梨県",   "lat": 35.6639, "lng": 138.5685, "tz": "Asia/Tokyo"},
    "JP.20": {"name": "長野県",   "lat": 36.6513, "lng": 138.1810, "tz": "Asia/Tokyo"},
    "JP.21": {"name": "岐阜県",   "lat": 35.3912, "lng": 136.7223, "tz": "Asia/Tokyo"},
    "JP.22": {"name": "静岡県",   "lat": 34.9769, "lng": 138.3831, "tz": "Asia/Tokyo"},
    "JP.23": {"name": "愛知県",   "lat": 35.1802, "lng": 136.9066, "tz": "Asia/Tokyo"},
    "JP.24": {"name": "三重県",   "lat": 34.7303, "lng": 136.5086, "tz": "Asia/Tokyo"},
    "JP.25": {"name": "滋賀県",   "lat": 35.0045, "lng": 135.8686, "tz": "Asia/Tokyo"},
    "JP.26": {"name": "京都府",   "lat": 35.0211, "lng": 135.7556, "tz": "Asia/Tokyo"},
    "JP.27": {"name": "大阪府",   "lat": 34.6937, "lng": 135.5023, "tz": "Asia/Tokyo"},
    "JP.28": {"name": "兵庫県",   "lat": 34.6913, "lng": 135.1830, "tz": "Asia/Tokyo"},
    "JP.29": {"name": "奈良県",   "lat": 34.6851, "lng": 135.8325, "tz": "Asia/Tokyo"},
    "JP.30": {"name": "和歌山県", "lat": 34.2260, "lng": 135.1675, "tz": "Asia/Tokyo"},
    "JP.31": {"name": "鳥取県",   "lat": 35.5036, "lng": 134.2381, "tz": "Asia/Tokyo"},
    "JP.32": {"name": "島根県",   "lat": 35.4723, "lng": 133.0505, "tz": "Asia/Tokyo"},
    "JP.33": {"name": "岡山県",   "lat": 34.6618, "lng": 133.9344, "tz": "Asia/Tokyo"},
    "JP.34": {"name": "広島県",   "lat": 34.3963, "lng": 132.4596, "tz": "Asia/Tokyo"},
    "JP.35": {"name": "山口県",   "lat": 34.1859, "lng": 131.4714, "tz": "Asia/Tokyo"},
    "JP.36": {"name": "徳島県",   "lat": 34.0658, "lng": 134.5593, "tz": "Asia/Tokyo"},
    "JP.37": {"name": "香川県",   "lat": 34.3401, "lng": 134.0434, "tz": "Asia/Tokyo"},
    "JP.38": {"name": "愛媛県",   "lat": 33.8417, "lng": 132.7657, "tz": "Asia/Tokyo"},
    "JP.39": {"name": "高知県",   "lat": 33.5597, "lng": 133.5311, "tz": "Asia/Tokyo"},
    "JP.40": {"name": "福岡県",   "lat": 33.6064, "lng": 130.4183, "tz": "Asia/Tokyo"},
    "JP.41": {"name": "佐賀県",   "lat": 33.2494, "lng": 130.2989, "tz": "Asia/Tokyo"},
    "JP.42": {"name": "長崎県",   "lat": 32.7448, "lng": 129.8737, "tz": "Asia/Tokyo"},
    "JP.43": {"name": "熊本県",   "lat": 32.7898, "lng": 130.7417, "tz": "Asia/Tokyo"},
    "JP.44": {"name": "大分県",   "lat": 33.2382, "lng": 131.6126, "tz": "Asia/Tokyo"},
    "JP.45": {"name": "宮崎県",   "lat": 31.9111, "lng": 131.4239, "tz": "Asia/Tokyo"},
    "JP.46": {"name": "鹿児島県", "lat": 31.5602, "lng": 130.5580, "tz": "Asia/Tokyo"},
    "JP.47": {"name": "沖縄県",   "lat": 26.2124, "lng": 127.6809, "tz": "Asia/Tokyo"},
}

# ── 天気コード変換 ───────────────────────────────────────────────────────────

# 判定に使用する時間（UTC → JST換算後のインデックス）
HOUR_INDICES = (6, 12, 18)

WEATHER_CATEGORIES = {
    "晴れ":  (0, 1),
    "くもり": (2, 3, 45, 48),
    "雨":    (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 97, 98, 99),
    "雪":    (71, 73, 75, 76, 77, 85, 86),
}

# weathercode → カテゴリの逆引きマップ（高速化）
_CODE_TO_CATEGORY: dict[int, str] = {}
for _cat, _codes in WEATHER_CATEGORIES.items():
    for _c in _codes:
        _CODE_TO_CATEGORY[_c] = _cat


def weathercode_to_category(code: int) -> str:
    """weathercode をカテゴリ文字列（晴れ/くもり/雨/雪）に変換する。
    想定外のコードは 'くもり' にフォールバック。"""
    return _CODE_TO_CATEGORY.get(code, "くもり")


def build_weather_string(morning: str, noon: str, evening: str) -> str:
    """朝・昼・夕の3カテゴリから表示用天気文字列を組み立てる。

    A == B == C         → 「A」
    A == B != C         → 「AのちC」
    A != B == C         → 「AのちB」
    A == C != B         → 「A一時B」
    A != B かつ B != C  → 「AのちBのちC」
    """
    a, b, c = morning, noon, evening
    if a == b == c:
        return a
    if a == b and b != c:
        return f"{a}のち{c}"
    if a != b and b == c:
        return f"{a}のち{b}"
    if a == c and a != b:
        return f"{a}一時{b}"
    return f"{a}のち{b}のち{c}"


# ── 座標導出 ─────────────────────────────────────────────────────────────────

def get_coords_for_prefecture(code: str) -> tuple[float, float, str]:
    """都道府県コードから (latitude, longitude, timezone) を返す。
    不明なコードの場合は ValueError。"""
    entry = PREFECTURE_MASTER.get(code)
    if entry is None:
        raise ValueError(f"不明な都道府県コードです: {code}")
    return entry["lat"], entry["lng"], entry["tz"]


def get_prefecture_name(code: str) -> Optional[str]:
    """都道府県コードから都道府県名を返す。不明な場合は None。"""
    entry = PREFECTURE_MASTER.get(code)
    return entry["name"] if entry else None


# ── 日付バリデーション ───────────────────────────────────────────────────────

def validate_date_range(start_date_str: str, end_date_str: str) -> bool:
    """日付範囲が API の制約内か検証する。

    制約: start_date >= 今日 - 366日, end_date <= 今日 - 1日
    戻り値: 有効なら True, 無効なら False
    """
    try:
        today = date.today()
        start_date = date.fromisoformat(start_date_str)
        end_date = date.fromisoformat(end_date_str)
        oldest = today - timedelta(days=366)
        if start_date < oldest:
            return False
        if end_date > today - timedelta(days=1):
            return False
        if start_date > end_date:
            return False
        return True
    except (ValueError, TypeError):
        return False


# ── Open-Meteo API 取得 ──────────────────────────────────────────────────────

OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


async def fetch_weather_from_api(
    lat: float,
    lng: float,
    start_date: str,
    end_date: str,
    timezone: str = "Asia/Tokyo",
) -> dict:
    """Open-Meteo Historical Weather API から hourly weathercode を取得する。

    戻り値:
        { "success": True, "data": { "YYYY-MM-DD": ["晴れ", "くもり", "晴れ"] }, ... }
        または
        { "success": False, "error": "..." }

    各日付のデータは [朝カテゴリ, 昼カテゴリ, 夕カテゴリ] の3要素。
    """
    if not validate_date_range(start_date, end_date):
        return {"success": False, "error": "日付範囲が無効です"}

    params = {
        "latitude": lat,
        "longitude": lng,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": "weathercode",
        "timezone": timezone,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(OPEN_METEO_ARCHIVE_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as e:
        logger.error("Open-Meteo API エラー: %s", e)
        return {"success": False, "error": "Open-Meteo APIの取得に失敗しました"}
    except Exception as e:
        logger.error("Open-Meteo API 予期せぬエラー: %s", e)
        return {"success": False, "error": "Open-Meteo APIの取得に失敗しました"}

    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    codes = hourly.get("weathercode", [])

    if not times or not codes:
        return {"success": False, "error": "APIレスポンスに天気データが含まれていません"}

    if len(codes) < 24:
        logger.warning(
            "hourlyデータが不完全です（配列長: %d）。start=%s end=%s",
            len(codes), start_date, end_date,
        )
        return {"success": False, "error": "天気データが不完全です（24時間未満）"}

    # 日付ごとに hours[6], hours[12], hours[18] を抽出してカテゴリ変換
    result: dict[str, list[str]] = {}
    try:
        start_dt = date.fromisoformat(start_date)
        end_dt = date.fromisoformat(end_date)
    except ValueError:
        return {"success": False, "error": "日付形式が不正です"}

    day_count = (end_dt - start_dt).days + 1
    for day_offset in range(day_count):
        day_date = start_dt + timedelta(days=day_offset)
        day_str = day_date.isoformat()
        base = day_offset * 24

        if base + 18 >= len(codes):
            logger.warning(
                "日付 %s のhourlyデータが不足しています（base=%d, len=%d）",
                day_str, base, len(codes),
            )
            continue

        cat_morning = weathercode_to_category(codes[base + 6])
        cat_noon = weathercode_to_category(codes[base + 12])
        cat_evening = weathercode_to_category(codes[base + 18])
        result[day_str] = [cat_morning, cat_noon, cat_evening]

    if not result:
        return {"success": False, "error": "有効な天気データを抽出できませんでした"}

    return {"success": True, "data": result}


def compute_weather_display(categories: list[str]) -> str:
    """3カテゴリのリストから表示用天気文字列を計算する。
    [朝, 昼, 夕] の順。"""
    if len(categories) != 3:
        return "くもり"
    return build_weather_string(categories[0], categories[1], categories[2])

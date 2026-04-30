# test/test_weather_api.py
"""天気APIエンドポイントの統合テスト"""
import pytest
from httpx import AsyncClient, ASGITransport
from main import app
from database import init_db, upsert_weather_log


@pytest.fixture
def _db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_weather.db"
    monkeypatch.setattr("database.DB_PATH", str(db_path))
    init_db()


@pytest.fixture
def _auth(monkeypatch):
    """認証をスキップ"""
    async def _skip(_req):
        pass
    monkeypatch.setattr("main.require_auth", _skip)


@pytest.mark.asyncio
async def test_get_weather_has_data(_db, _auth):
    """データがある日付の取得（GET /api/weather?date=...）"""
    upsert_weather_log("2026-04-15", "晴れのちくもり", "2026-04-16 09:00:00")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/weather", params={"date": "2026-04-15"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["date"] == "2026-04-15"
    assert data["weather"] == "晴れのちくもり"
    assert data["recorded_at"] == "2026-04-16 09:00:00"


@pytest.mark.asyncio
async def test_get_weather_no_data(_db, _auth):
    """データがない日付の取得"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/weather", params={"date": "2026-04-15"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["date"] == "2026-04-15"
    assert data["weather"] is None
    assert data["recorded_at"] is None


@pytest.mark.asyncio
async def test_get_weather_range(_db, _auth):
    """期間取得（GET /api/weather/range）"""
    upsert_weather_log("2026-04-10", "晴れ", "2026-04-11 08:00:00")
    upsert_weather_log("2026-04-11", "くもり", "2026-04-12 08:00:00")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/weather/range",
            params={"start": "2026-04-10", "end": "2026-04-11"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["start"] == "2026-04-10"
    assert data["end"] == "2026-04-11"
    assert data["count"] == 2
    assert data["data"][0]["weather"] == "晴れ"
    assert data["data"][1]["weather"] == "くもり"


@pytest.mark.asyncio
async def test_post_weather_fetch_validation_error(_db, _auth):
    """当日リクエスト時のバリデーションエラー"""
    from datetime import date as _date
    today = _date.today().isoformat()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/weather/fetch",
            json={"date": today},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "invalid"

"""
カロリー検索モジュール
検索順: カロリーSlism → 空リスト返却（Claude推定へフォールバック）

Slism 検索仕様（2024年調査済み）:
  URL    : https://calorie.slism.jp/?searchWord={food_name}&search=検索
  結果表  : table.searchItemList (または table.searchItemArea) > tr.searchItem
  データ  : 各行の hidden input から取得
              searchNameVal   ... 食品名
              searchKcalVal   ... カロリー（デフォルト分量あたり）
              searchNo        ... 食品ID（個別ページ /FOODID/ に使用）
              searchAmount_comment  ... 分量説明（例: "65ml", "200ml"）
              searchAmount_original ... グラム換算値
  PFC/塩分: 個別ページ https://calorie.slism.jp/{searchNo}/ のテーブルから取得
"""
import asyncio
import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 8.0


def _to_float(text: str) -> float | None:
    """テキストから最初の数値を抽出"""
    m = re.search(r"[\d.]+", text.replace(",", ""))
    return float(m.group()) if m else None


# ── カロリーSlism ──────────────────────────────────────────────────────────────

async def search_slism(food_name: str) -> list[dict]:
    """
    カロリーSlism を検索して候補リストを返す。
    失敗時は空リストを返す（Claude推定へフォールバック）。
    """
    url = f"https://calorie.slism.jp/?searchWord={quote(food_name)}&search={quote('検索')}"
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=True, headers=HEADERS
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, "html.parser")

            # 検索結果テーブル: table.searchItemList または table.searchItemArea
            table = (
                soup.find("table", class_="searchItemList")
                or soup.find("table", class_="searchItemArea")
            )
            if not table:
                return []

            rows = table.find_all("tr", class_="searchItem")
            if not rows:
                return []

            # 個別ページPFC取得（上位5件のみ並行取得）
            top_rows = rows[:5]
            search_nos = []
            for row in top_rows:
                no_inp = row.find("input", attrs={"name": "searchNo"})
                search_nos.append(no_inp.get("value", "") if no_inp else "")

            async def _empty() -> dict:
                return {}

            detail_tasks = [
                _fetch_slism_detail(client, f"https://calorie.slism.jp/{no}/")
                if no else _empty()
                for no in search_nos
            ]
            details = await asyncio.gather(*detail_tasks, return_exceptions=True)

            results: list[dict] = []
            for i, row in enumerate(rows[:10]):
                # hidden inputs から情報取得
                def _inp(name: str) -> str:
                    el = row.find("input", attrs={"name": name})
                    return el.get("value", "") if el else ""

                name = _inp("searchNameVal")
                if not name:
                    continue
                cal_str = _inp("searchKcalVal")
                cal = _to_float(cal_str) if cal_str else None
                unit = _inp("searchAmount_comment")  # 例: "65ml", "200ml"

                # PFC（個別ページ取得済み分のみ）
                detail = details[i] if i < len(details) and isinstance(details[i], dict) else {}

                results.append({
                    "name": name,
                    "unit": unit,
                    "calories": cal,
                    "protein": detail.get("protein"),
                    "fat": detail.get("fat"),
                    "carbs": detail.get("carbs"),
                    "sodium": detail.get("sodium"),
                    "source": "slism",
                })

            return results

    except Exception:
        return []


async def _fetch_slism_detail(client: httpx.AsyncClient, url: str) -> dict:
    """Slism個別ページからPFC・塩分相当量を取得"""
    if not url:
        return {}
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return {}
        soup = BeautifulSoup(resp.text, "html.parser")
        result: dict = {}

        for row in soup.select("table tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(strip=True)
            value = _to_float(cells[1].get_text(strip=True))
            if value is None:
                continue
            if "タンパク質" in label or "たんぱく質" in label:
                result["protein"] = value
            elif "脂質" in label:
                result["fat"] = value
            elif "炭水化物" in label:
                result["carbs"] = value
            elif "食塩相当量" in label:
                result["sodium"] = value

        return result
    except Exception:
        return {}


# ── 統合検索エントリーポイント ─────────────────────────────────────────────────

async def search_nutrition(food_name: str, amount: str = "") -> dict:
    """
    Slism で検索し、結果を返す。見つからない場合は空リストを返す（Claude推定へフォールバック）。

    Returns:
        {
            "found": bool,
            "source": "slism" | "none",
            "foods": [{"name", "unit", "calories", "protein", "fat", "carbs", "sodium"}, ...]
        }
    """
    foods = await search_slism(food_name)
    if foods:
        return {"found": True, "source": "slism", "foods": foods}

    return {"found": False, "source": "none", "foods": []}

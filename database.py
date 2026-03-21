import json
import sqlite3
from datetime import datetime, timezone, timedelta, date as _date
from pathlib import Path
from typing import Optional

JST = timezone(timedelta(hours=9))


def today_jst() -> str:
    """JSTの今日の日付をYYYY-MM-DD形式で返す"""
    return datetime.now(JST).date().isoformat()

DB_PATH = Path(__file__).parent / "health.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                meal_date   DATE NOT NULL,
                meal_type   TEXT NOT NULL,
                description TEXT NOT NULL,
                calories    INTEGER,
                protein     REAL,
                fat         REAL,
                carbs       REAL,
                sodium      REAL,
                notes       TEXT
            );

            CREATE TABLE IF NOT EXISTS weight_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                log_date    DATE NOT NULL,
                time_of_day TEXT NOT NULL,
                weight_kg   REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS steps_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                log_date    DATE NOT NULL,
                steps       INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meal_images (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                meal_id     INTEGER REFERENCES meals(id),
                image_data  BLOB NOT NULL,
                mime_type   TEXT NOT NULL,
                source_type TEXT NOT NULL,
                notes       TEXT
            );

            CREATE TABLE IF NOT EXISTS food_defaults (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                keyword     TEXT NOT NULL,
                description TEXT NOT NULL,
                notes       TEXT
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                role         TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS conversation_summary (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_text  TEXT NOT NULL,
                covered_up_to INTEGER,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 初期設定
        conn.executemany(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            [
                ("daily_calorie_goal", "1800"),
                ("user_name", "DefaultName"),
                ("user_height_cm", "160"),
                ("app_password", "1234"),
                ("user_notes", ""),
                ("savings_mode", "false"),
            ],
        )



# ── 設定 ──────────────────────────────────────────────────────────────────────

def get_setting(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def save_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )


# ── 会話履歴永続化 ────────────────────────────────────────────────────────────

def save_conversation_message(role: str, content: list) -> int:
    """会話メッセージをDBに保存し、IDを返す"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO conversation_messages (role, content_json) VALUES (?, ?)",
            (role, json.dumps(content, ensure_ascii=False)),
        )
        return cur.lastrowid


def load_recent_conversation(limit: int = 10) -> list[dict]:
    """直近N件の会話メッセージをDBから読み込む（古い順）"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT role, content_json FROM (
                SELECT id, role, content_json FROM conversation_messages
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (limit,),
        ).fetchall()
    return [{"role": r["role"], "content": json.loads(r["content_json"])} for r in rows]


def trim_conversation_history(keep: int = 10):
    """直近N件のみ残して古いメッセージを削除する"""
    with get_conn() as conn:
        conn.execute(
            """
            DELETE FROM conversation_messages
            WHERE id NOT IN (
                SELECT id FROM conversation_messages ORDER BY id DESC LIMIT ?
            )
            """,
            (keep,),
        )


def clear_conversation_history():
    """会話履歴を全件削除する"""
    with get_conn() as conn:
        conn.execute("DELETE FROM conversation_messages")


def save_conversation_summary(summary_text: str, covered_up_to: Optional[int] = None):
    """会話サマリーを保存（1件のみ保持、上書き）"""
    with get_conn() as conn:
        conn.execute("DELETE FROM conversation_summary")
        conn.execute(
            "INSERT INTO conversation_summary (summary_text, covered_up_to) VALUES (?, ?)",
            (summary_text, covered_up_to),
        )


def load_conversation_summary() -> Optional[str]:
    """最新の会話サマリーを返す"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT summary_text FROM conversation_summary ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row["summary_text"] if row else None


def get_latest_conversation_message_id() -> Optional[int]:
    """最新の会話メッセージIDを返す"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS max_id FROM conversation_messages"
        ).fetchone()
    return row["max_id"] if row else None



# ── 食品デフォルト ─────────────────────────────────────────────────────────────

def get_food_defaults() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT keyword, description, notes FROM food_defaults ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_food_default(keyword: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM food_defaults WHERE keyword = ?", (keyword,))
        return cur.rowcount > 0


def save_food_default(keyword: str, description: str, notes: Optional[str] = None):
    """food_defaultsに保存（同一keywordは上書き）"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM food_defaults WHERE keyword = ?", (keyword,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE food_defaults SET description=?, notes=?, updated_at=CURRENT_TIMESTAMP WHERE keyword=?",
                (description, notes, keyword),
            )
        else:
            conn.execute(
                "INSERT INTO food_defaults (keyword, description, notes) VALUES (?, ?, ?)",
                (keyword, description, notes),
            )


# ── 食事記録 ───────────────────────────────────────────────────────────────────

def save_meal(
    meal_date: str,
    meal_type: str,
    description: str,
    calories: Optional[int] = None,
    protein: Optional[float] = None,
    fat: Optional[float] = None,
    carbs: Optional[float] = None,
    sodium: Optional[float] = None,
    notes: Optional[str] = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO meals
                (meal_date, meal_type, description, calories, protein, fat, carbs, sodium, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (meal_date, meal_type, description, calories, protein, fat, carbs, sodium, notes),
        )
        return cur.lastrowid


def update_meal(meal_id: int, **kwargs) -> bool:
    allowed = {"description", "meal_type", "calories", "protein", "fat", "carbs", "sodium", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [meal_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE meals SET {set_clause} WHERE id = ?", values)
    return True


def get_meals_by_date(meal_date: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, meal_type, description, calories, protein, fat, carbs, sodium, notes
            FROM meals WHERE meal_date = ? ORDER BY recorded_at
            """,
            (meal_date,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_meals_by_date_and_type(meal_date: str, meal_type: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, description, calories, protein, fat, carbs, sodium, notes
            FROM meals WHERE meal_date = ? AND meal_type = ? ORDER BY recorded_at
            """,
            (meal_date, meal_type),
        ).fetchall()
        return [dict(r) for r in rows]


# ── 体重記録 ───────────────────────────────────────────────────────────────────

def save_weight(log_date: str, time_of_day: str, weight_kg: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO weight_logs (log_date, time_of_day, weight_kg) VALUES (?, ?, ?)",
            (log_date, time_of_day, weight_kg),
        )
        return cur.lastrowid


def get_previous_weight(time_of_day: str, before_date: Optional[str] = None) -> Optional[float]:
    """同じ時間帯の直前の体重を返す"""
    before_date = before_date or today_jst()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT weight_kg FROM weight_logs
            WHERE time_of_day = ? AND log_date < ?
            ORDER BY log_date DESC LIMIT 1
            """,
            (time_of_day, before_date),
        ).fetchone()
        return row["weight_kg"] if row else None


# ── 歩数記録 ───────────────────────────────────────────────────────────────────

def save_steps(log_date: str, steps: int) -> dict:
    """保存（同日レコードがあれば上書き）。結果を返す。"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, steps FROM steps_logs WHERE log_date = ?", (log_date,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE steps_logs SET steps = ?, recorded_at = CURRENT_TIMESTAMP WHERE log_date = ?",
                (steps, log_date),
            )
            return {"id": existing["id"], "updated": True, "previous_steps": existing["steps"]}
        else:
            cur = conn.execute(
                "INSERT INTO steps_logs (log_date, steps) VALUES (?, ?)", (log_date, steps)
            )
            return {"id": cur.lastrowid, "updated": False}


# ── 日次サマリー ───────────────────────────────────────────────────────────────

# ── 食事画像 ───────────────────────────────────────────────────────────────────

def save_meal_image(
    meal_id: int,
    image_data: bytes,
    mime_type: str,
    source_type: str,
    notes: Optional[str] = None,
) -> int:
    """
    meal_images テーブルに画像を保存する。
    source_type: 'photo'（料理写真）/ 'label'（栄養成分ラベル）/ 'barcode'（バーコード）
    """
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO meal_images (meal_id, image_data, mime_type, source_type, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (meal_id, image_data, mime_type, source_type, notes),
        )
        return cur.lastrowid


def get_meal_image(meal_id: int) -> Optional[tuple[bytes, str]]:
    """食事に紐づく最初の画像を (image_data, mime_type) で返す"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT image_data, mime_type FROM meal_images WHERE meal_id = ? LIMIT 1",
            (meal_id,),
        ).fetchone()
    return (bytes(row["image_data"]), row["mime_type"]) if row else None


def get_history(
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """指定期間の記録を日付降順で返す"""
    today = datetime.now(JST).date()
    if start_date and end_date:
        since = start_date
        until = end_date
    else:
        until = today.isoformat()
        since = (today - timedelta(days=days - 1)).isoformat()
    with get_conn() as conn:
        meals = conn.execute(
            """
            SELECT m.id, m.meal_date, m.meal_type, m.description,
                   m.calories, m.protein, m.fat, m.carbs, m.sodium, m.notes,
                   (SELECT COUNT(*) FROM meal_images WHERE meal_id = m.id) AS image_count
            FROM meals m
            WHERE m.meal_date >= ? AND m.meal_date <= ?
            ORDER BY m.meal_date DESC, m.id ASC
            """,
            (since, until),
        ).fetchall()
        weights = conn.execute(
            "SELECT log_date, time_of_day, weight_kg FROM weight_logs WHERE log_date >= ? AND log_date <= ? ORDER BY log_date DESC",
            (since, until),
        ).fetchall()
        steps_rows = conn.execute(
            "SELECT log_date, steps FROM steps_logs WHERE log_date >= ? AND log_date <= ? ORDER BY log_date DESC",
            (since, until),
        ).fetchall()

    from collections import defaultdict
    days_map: dict = defaultdict(lambda: {"meals": [], "weight": {}, "steps": None})
    for m in meals:
        days_map[m["meal_date"]]["meals"].append(dict(m))
    for w in weights:
        days_map[w["log_date"]]["weight"][w["time_of_day"]] = w["weight_kg"]
    for s in steps_rows:
        days_map[s["log_date"]]["steps"] = s["steps"]

    result = []
    for date in sorted(days_map.keys(), reverse=True):
        d = days_map[date]
        ml = d["meals"]
        result.append({
            "date": date,
            "meals": ml,
            "weight": d["weight"],
            "steps": d["steps"],
            "totals": {
                "calories": int(sum(m.get("calories") or 0 for m in ml)),
                "protein": round(sum(m.get("protein") or 0 for m in ml), 1),
                "fat": round(sum(m.get("fat") or 0 for m in ml), 1),
                "carbs": round(sum(m.get("carbs") or 0 for m in ml), 1),
                "sodium": round(sum(m.get("sodium") or 0 for m in ml), 2),
            },
        })
    return result


def get_stats(
    days: int = 7,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """グラフ用の集計データを返す"""
    today = datetime.now(JST).date()
    if start_date and end_date:
        start = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date)
    else:
        end = today
        start = today - timedelta(days=days - 1)
    total = (end - start).days + 1
    if total > 366:  # 最大1年
        start = end - timedelta(days=365)
        total = 366
    dates = [(start + timedelta(days=i)).isoformat() for i in range(total)]
    since = dates[0]
    until = dates[-1]
    with get_conn() as conn:
        cal_rows = conn.execute(
            """
            SELECT meal_date,
                   SUM(calories) AS cal, SUM(protein) AS p,
                   SUM(fat) AS f, SUM(carbs) AS c
            FROM meals WHERE meal_date >= ? AND meal_date <= ?
            GROUP BY meal_date
            """,
            (since, until),
        ).fetchall()
        weight_rows = conn.execute(
            "SELECT log_date, time_of_day, weight_kg FROM weight_logs WHERE log_date >= ? AND log_date <= ?",
            (since, until),
        ).fetchall()
        step_rows = conn.execute(
            "SELECT log_date, steps FROM steps_logs WHERE log_date >= ? AND log_date <= ?",
            (since, until),
        ).fetchall()

    cal_map = {r["meal_date"]: r for r in cal_rows}
    w_map: dict = {}
    for r in weight_rows:
        w_map.setdefault(r["log_date"], {})[r["time_of_day"]] = r["weight_kg"]
    s_map = {r["log_date"]: r["steps"] for r in step_rows}

    calories, protein, fat, carbs = [], [], [], []
    wm, we, steps = [], [], []
    for d in dates:
        c = cal_map.get(d)
        calories.append(int(c["cal"]) if c and c["cal"] is not None else None)
        protein.append(round(c["p"], 1) if c and c["p"] is not None else None)
        fat.append(round(c["f"], 1) if c and c["f"] is not None else None)
        carbs.append(round(c["c"], 1) if c and c["c"] is not None else None)
        w = w_map.get(d, {})
        wm.append(w.get("morning"))
        we.append(w.get("evening"))
        steps.append(s_map.get(d))

    return {
        "period": total,
        "dates": dates,
        "calories": calories,
        "calories_goal": int(get_setting("daily_calorie_goal") or 1500),
        "weights_morning": wm,
        "weights_evening": we,
        "steps": steps,
        "protein": protein,
        "fat": fat,
        "carbs": carbs,
    }


def get_report_weeks() -> list[dict]:
    """記録が存在する週（日曜〜土曜）のリストを降順で返す"""
    with get_conn() as conn:
        dates: set[str] = set()
        for row in conn.execute("SELECT DISTINCT meal_date FROM meals").fetchall():
            dates.add(row["meal_date"])
        for row in conn.execute("SELECT DISTINCT log_date FROM weight_logs").fetchall():
            dates.add(row["log_date"])
        for row in conn.execute("SELECT DISTINCT log_date FROM steps_logs").fetchall():
            dates.add(row["log_date"])
    if not dates:
        return []
    weeks: set[tuple[str, str]] = set()
    for d in dates:
        dt = _date.fromisoformat(d)
        days_since_sunday = (dt.weekday() + 1) % 7
        sunday = dt - timedelta(days=days_since_sunday)
        saturday = sunday + timedelta(days=6)
        weeks.add((sunday.isoformat(), saturday.isoformat()))
    return sorted(
        [{"start": s, "end": e} for s, e in weeks],
        key=lambda x: x["start"],
        reverse=True,
    )


def get_report_data(start_date: str, end_date: str) -> dict:
    """レポート用1週間データを取得"""
    with get_conn() as conn:
        meals = conn.execute(
            """
            SELECT meal_date, meal_type, description,
                   calories, protein, fat, carbs, sodium
            FROM meals WHERE meal_date BETWEEN ? AND ?
            ORDER BY meal_date, meal_type, recorded_at
            """,
            (start_date, end_date),
        ).fetchall()
        weights = conn.execute(
            "SELECT log_date, time_of_day, weight_kg FROM weight_logs WHERE log_date BETWEEN ? AND ? ORDER BY log_date",
            (start_date, end_date),
        ).fetchall()
        steps_rows = conn.execute(
            "SELECT log_date, steps FROM steps_logs WHERE log_date BETWEEN ? AND ?",
            (start_date, end_date),
        ).fetchall()

    meal_map: dict = {}
    for m in meals:
        meal_map.setdefault((m["meal_date"], m["meal_type"]), []).append(dict(m))
    w_map: dict = {}
    for w in weights:
        w_map.setdefault(w["log_date"], {})[w["time_of_day"]] = w["weight_kg"]
    s_map = {r["log_date"]: r["steps"] for r in steps_rows}

    MEAL_TYPES = ["breakfast", "lunch", "dinner", "snack", "late_night"]
    start = _date.fromisoformat(start_date)
    days = []
    for i in range(7):
        d = (start + timedelta(days=i)).isoformat()
        day_meals = {mt: meal_map.get((d, mt), []) for mt in MEAL_TYPES}
        all_m = [m for ms in day_meals.values() for m in ms]
        cal = sum(m.get("calories") or 0 for m in all_m) if all_m else None
        p   = round(sum(m.get("protein") or 0 for m in all_m), 1) if all_m else None
        f   = round(sum(m.get("fat")     or 0 for m in all_m), 1) if all_m else None
        c   = round(sum(m.get("carbs")   or 0 for m in all_m), 1) if all_m else None
        sod = round(sum(m.get("sodium")  or 0 for m in all_m), 2) if all_m else None
        days.append({
            "date": d,
            "meals": day_meals,
            "calories": cal,
            "protein": p, "fat": f, "carbs": c, "sodium": sod,
            "weight_morning": w_map.get(d, {}).get("morning"),
            "weight_evening": w_map.get(d, {}).get("evening"),
            "steps": s_map.get(d),
        })

    return {
        "start": start_date,
        "end": end_date,
        "days": days,
        "user_name":    get_setting("user_name") or "—",
        "height_cm":    get_setting("user_height_cm") or "—",
        "calorie_goal": int(get_setting("daily_calorie_goal") or 1500),
    }


def get_daily_summary(target_date: Optional[str] = None) -> dict:
    target_date = target_date or today_jst()
    with get_conn() as conn:
        meals = conn.execute(
            """
            SELECT id, meal_type, description, calories, protein, fat, carbs, sodium, notes
            FROM meals WHERE meal_date = ? ORDER BY recorded_at
            """,
            (target_date,),
        ).fetchall()

        weights = conn.execute(
            "SELECT time_of_day, weight_kg FROM weight_logs WHERE log_date = ?",
            (target_date,),
        ).fetchall()

        steps_row = conn.execute(
            "SELECT steps FROM steps_logs WHERE log_date = ?", (target_date,)
        ).fetchone()

    total_cal = sum(r["calories"] or 0 for r in meals)
    total_p = sum(r["protein"] or 0 for r in meals)
    total_f = sum(r["fat"] or 0 for r in meals)
    total_c = sum(r["carbs"] or 0 for r in meals)
    total_s = sum(r["sodium"] or 0 for r in meals)

    return {
        "date": target_date,
        "meals": [dict(m) for m in meals],
        "weight": {r["time_of_day"]: r["weight_kg"] for r in weights},
        "steps": steps_row["steps"] if steps_row else None,
        "totals": {
            "calories": total_cal,
            "protein": round(total_p, 1),
            "fat": round(total_f, 1),
            "carbs": round(total_c, 1),
            "sodium": round(total_s, 1),
        },
    }

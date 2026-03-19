import sqlite3
from datetime import datetime, timezone, timedelta
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

        # 初期設定
        conn.executemany(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            [
                ("daily_calorie_goal", "1500"),
                ("user_name", "Serick"),
                ("user_height_cm", "180"),
                ("app_password", "1234"),
            ],
        )

        # 食品デフォルト初期データ
        initial_defaults = [
            ("鶏胸肉", "皮無し", None),
            ("鶏むね肉", "皮無し", None),
            ("オートミール", "味の素製のロールドオーツ", None),
        ]
        for keyword, description, notes in initial_defaults:
            conn.execute(
                """
                INSERT INTO food_defaults (keyword, description, notes)
                SELECT ?, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM food_defaults WHERE keyword = ?)
                """,
                (keyword, description, notes, keyword),
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


# ── 食品デフォルト ─────────────────────────────────────────────────────────────

def get_food_defaults() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT keyword, description, notes FROM food_defaults ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


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

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta, date as _date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

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


def _run_migrations(conn: sqlite3.Connection, migrations: list) -> None:
    """既存DBへのカラム追加等のマイグレーションを安全に実行する。
    duplicate column name エラーは既適用として無視し、それ以外は再送出する。
    """
    for sql in migrations:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                logger.debug(f"Migration skip (already applied): {sql}")
            else:
                logger.error(f"Migration failed: {sql} — {e}")
                raise


def _migrate_meal_images_nullable(conn: sqlite3.Connection) -> None:
    """meal_images.image_data の NOT NULL 制約を撤廃するテーブル再作成マイグレーション。
    PRAGMA table_info で notnull=1 の場合のみ実行。既に NULL 許容なら即リターン。
    """
    info = conn.execute("PRAGMA table_info(meal_images)").fetchall()
    for col in info:
        if col["name"] == "image_data" and col["notnull"] == 0:
            return  # 既にNULL許容 → スキップ

    logger.info("Migration: recreating meal_images to allow NULL image_data")
    conn.executescript("""
        BEGIN;
        ALTER TABLE meal_images RENAME TO meal_images_old;
        CREATE TABLE meal_images (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            meal_id     INTEGER REFERENCES meals(id),
            image_data  BLOB DEFAULT NULL,
            mime_type   TEXT NOT NULL,
            source_type TEXT NOT NULL,
            notes       TEXT,
            image_path  TEXT DEFAULT NULL
        );
        INSERT INTO meal_images
            (id, recorded_at, meal_id, image_data, mime_type, source_type, notes, image_path)
        SELECT
            id, recorded_at, meal_id, image_data, mime_type, source_type, notes, image_path
        FROM meal_images_old;
        DROP TABLE meal_images_old;
        COMMIT;
    """)
    logger.info("Migration: meal_images recreated successfully")


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
                notes       TEXT,
                meal_time   TEXT DEFAULT NULL
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

            CREATE TABLE IF NOT EXISTS meal_skips (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                meal_date   DATE NOT NULL,
                meal_type   TEXT NOT NULL,
                UNIQUE(meal_date, meal_type)
            );

            CREATE TABLE IF NOT EXISTS sleep_logs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                date             TEXT    NOT NULL,
                sleep_start      TEXT    NOT NULL,
                sleep_end        TEXT    NOT NULL,
                duration_minutes INTEGER DEFAULT NULL,
                deep_minutes     INTEGER DEFAULT NULL,
                rem_minutes      INTEGER DEFAULT NULL,
                awake_minutes    INTEGER DEFAULT NULL,
                source           TEXT    DEFAULT 'healthkit'
                                         CHECK(source IN ('healthkit', 'manual')),
                recorded_at      TEXT    DEFAULT (datetime('now','localtime')),
                UNIQUE(date)
            );

            CREATE TABLE IF NOT EXISTS vitals_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT    NOT NULL,
                time        TEXT    DEFAULT NULL,
                type        TEXT    NOT NULL
                                    CHECK(type IN ('heart_rate', 'spo2', 'bp_alert')),
                value       REAL    DEFAULT NULL,
                note        TEXT    DEFAULT NULL,
                source      TEXT    DEFAULT 'healthkit',
                recorded_at TEXT    DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS meal_images (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                meal_id     INTEGER REFERENCES meals(id),
                image_data  BLOB DEFAULT NULL,
                mime_type   TEXT NOT NULL,
                source_type TEXT NOT NULL,
                notes       TEXT,
                image_path  TEXT DEFAULT NULL
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

        # インデックス（既存DBでも安全に追加）
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_meals_date       ON meals(meal_date);
            CREATE INDEX IF NOT EXISTS idx_weight_date      ON weight_logs(log_date);
            CREATE INDEX IF NOT EXISTS idx_steps_date       ON steps_logs(log_date);
            CREATE INDEX IF NOT EXISTS idx_meal_skips_date  ON meal_skips(meal_date);
            CREATE INDEX IF NOT EXISTS idx_sleep_logs_date  ON sleep_logs(date);
            CREATE INDEX IF NOT EXISTS idx_vitals_logs_date ON vitals_logs(date);
            CREATE INDEX IF NOT EXISTS idx_vitals_logs_type ON vitals_logs(type);
        """)

        # 既存DBへのカラム追加マイグレーション
        _run_migrations(conn, [
            "ALTER TABLE food_defaults ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE meals ADD COLUMN meal_time TEXT DEFAULT NULL",
            "ALTER TABLE meal_images ADD COLUMN image_path TEXT DEFAULT NULL",
        ])

        # image_data NOT NULL → NULL 許容へのテーブル再作成マイグレーション
        # （新規DBは CREATE TABLE で既に NULL 許容のためスキップされる）
        _migrate_meal_images_nullable(conn)

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
                ("normal_model", "claude-sonnet-4-6"),
                ("savings_model", "claude-haiku-4-5-20251001"),
                ("cache_ttl", "5min"),
                ("use_food_defaults", "true"),
                ("auto_save_food_defaults", "true"),
                ("split_multiple_items", "false"),
                ("theme", "auto"),
                ("external_api_key", ""),
                ("daily_steps_goal", "8000"),
                ("day_start_hour", "4"),
                ("password_disabled", "false"),
                ("user_gender", ""),
                ("user_birthdate", ""),
            ],
        )

        # steps_api_key → external_api_key 自動移行（既存ユーザーの再設定不要）
        old_row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'steps_api_key'"
        ).fetchone()
        new_row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'external_api_key'"
        ).fetchone()
        if old_row and old_row["value"] and new_row and not new_row["value"]:
            conn.execute(
                "UPDATE app_settings SET value = ? WHERE key = 'external_api_key'",
                (old_row["value"],),
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


def get_logical_today_jst() -> str:
    """day_start_hour 設定を考慮した論理上の今日の日付を YYYY-MM-DD 形式で返す。
    例: day_start_hour=4 かつ現在時刻が 02:30 → 前日を返す（0:00-3:59 は前日扱い）"""
    hour = int(get_setting("day_start_hour") or "4")
    now = datetime.now(JST)
    if now.hour < hour:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


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
            "SELECT keyword, description, notes, is_favorite FROM food_defaults ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def get_favorite_food_defaults() -> list[dict]:
    """お気に入りのfood_defaultsを返す"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT keyword, description, notes, is_favorite FROM food_defaults WHERE is_favorite = 1 ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def toggle_food_default_favorite(keyword: str) -> Optional[bool]:
    """お気に入り状態をトグル。更新後の状態(bool)を返す。見つからなければNone"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_favorite FROM food_defaults WHERE keyword = ?", (keyword,)
        ).fetchone()
        if row is None:
            return None
        new_val = 0 if row["is_favorite"] else 1
        conn.execute(
            "UPDATE food_defaults SET is_favorite=?, updated_at=CURRENT_TIMESTAMP WHERE keyword=?",
            (new_val, keyword),
        )
        return bool(new_val)


def delete_food_default(keyword: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM food_defaults WHERE keyword = ?", (keyword,))
        return cur.rowcount > 0


def save_food_default(
    keyword: str,
    description: str,
    notes: Optional[str] = None,
    is_favorite: Optional[bool] = None,
):
    """food_defaultsに保存（同一keywordは上書き）"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, is_favorite FROM food_defaults WHERE keyword = ?", (keyword,)
        ).fetchone()
        if existing:
            fav = int(is_favorite) if is_favorite is not None else existing["is_favorite"]
            conn.execute(
                "UPDATE food_defaults SET description=?, notes=?, is_favorite=?, updated_at=CURRENT_TIMESTAMP WHERE keyword=?",
                (description, notes, fav, keyword),
            )
        else:
            fav = int(is_favorite) if is_favorite is not None else 0
            conn.execute(
                "INSERT INTO food_defaults (keyword, description, notes, is_favorite) VALUES (?, ?, ?, ?)",
                (keyword, description, notes, fav),
            )


def get_frequent_meals(days: int = 30, limit: int = 10) -> list[dict]:
    """直近N日間で頻出の食事をランキング形式で返す"""
    cutoff = (_date.fromisoformat(get_logical_today_jst()) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT description, meal_type, COUNT(*) AS cnt, AVG(calories) AS avg_cal
            FROM meals
            WHERE meal_date >= ?
            GROUP BY description
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (cutoff, limit),
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
    meal_time: Optional[str] = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO meals
                (meal_date, meal_type, description, calories, protein, fat, carbs, sodium, notes, meal_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (meal_date, meal_type, description, calories, protein, fat, carbs, sodium, notes, meal_time),
        )
        return cur.lastrowid


def update_meal(meal_id: int, **kwargs) -> bool:
    allowed = {"description", "meal_type", "calories", "protein", "fat", "carbs", "sodium", "notes", "meal_time"}
    # meal_time は NULL 更新（削除）を許容する
    nullable = {"meal_time"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and (v is not None or k in nullable)}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [meal_id]
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE meals SET {set_clause} WHERE id = ?", values)
    return cur.rowcount > 0


def update_meal_full(
    meal_id: int,
    meal_date: str,
    meal_type: str,
    description: str,
    calories: Optional[int],
    protein: Optional[float],
    fat: Optional[float],
    carbs: Optional[float],
    sodium: Optional[float],
    notes: Optional[str],
) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE meals SET
                meal_date=?, meal_type=?, description=?,
                calories=?, protein=?, fat=?, carbs=?, sodium=?, notes=?
            WHERE id=?
            """,
            (meal_date, meal_type, description, calories, protein, fat, carbs, sodium, notes, meal_id),
        )
    return cur.rowcount > 0


def delete_meal(meal_id: int) -> bool:
    with get_conn() as conn:
        conn.execute("DELETE FROM meal_images WHERE meal_id = ?", (meal_id,))
        cur = conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    return cur.rowcount > 0


def get_meals_by_date(meal_date: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, meal_type, description, calories, protein, fat, carbs, sodium, notes, meal_time
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


def upsert_weight(log_date: str, time_of_day: str, weight_kg: float) -> dict:
    """体重をUPSERT（同日同時間帯が存在すれば上書き）。戻り値: {"id": int, "updated": bool}"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM weight_logs WHERE log_date = ? AND time_of_day = ?",
            (log_date, time_of_day),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE weight_logs SET weight_kg = ?, recorded_at = CURRENT_TIMESTAMP WHERE id = ?",
                (weight_kg, existing["id"]),
            )
            return {"id": existing["id"], "updated": True}
        else:
            cur = conn.execute(
                "INSERT INTO weight_logs (log_date, time_of_day, weight_kg) VALUES (?, ?, ?)",
                (log_date, time_of_day, weight_kg),
            )
            return {"id": cur.lastrowid, "updated": False}


def update_weight_by_id(weight_id: int, weight_kg: float) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE weight_logs SET weight_kg=?, recorded_at=CURRENT_TIMESTAMP WHERE id=?",
            (weight_kg, weight_id),
        )
    return cur.rowcount > 0


def delete_weight_by_id(weight_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM weight_logs WHERE id=?", (weight_id,))
    return cur.rowcount > 0


def get_previous_weight(time_of_day: str, before_date: Optional[str] = None) -> Optional[float]:
    """同じ時間帯の直前の体重を返す"""
    before_date = before_date or get_logical_today_jst()
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


def update_steps_by_id(steps_id: int, steps: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE steps_logs SET steps=?, recorded_at=CURRENT_TIMESTAMP WHERE id=?",
            (steps, steps_id),
        )
    return cur.rowcount > 0


def delete_steps_by_id(steps_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM steps_logs WHERE id=?", (steps_id,))
    return cur.rowcount > 0


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
    meal_images テーブルに画像BLOBを保存する（既存互換）。
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


def save_meal_image_path(
    meal_id: int,
    image_path: str,
    mime_type: str,
    source_type: str,
    notes: Optional[str] = None,
) -> int:
    """
    meal_images テーブルにファイルパスのみを保存する（新規アップロード用）。
    image_data は保存しない。
    """
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO meal_images (meal_id, image_path, mime_type, source_type, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (meal_id, image_path, mime_type, source_type, notes),
        )
        return cur.lastrowid


def get_meal_image(meal_id: int) -> Optional[dict]:
    """食事に紐づく最初の画像レコードを dict で返す。
    キー: image_path, image_data, mime_type
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT image_data, mime_type, image_path FROM meal_images WHERE meal_id = ? LIMIT 1",
            (meal_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "image_path": row["image_path"],
        "image_data": bytes(row["image_data"]) if row["image_data"] else None,
        "mime_type": row["mime_type"],
    }


def get_meal_image_by_id(image_id: int) -> Optional[dict]:
    """image_id 指定で画像レコードを dict で返す。
    キー: image_path, image_data, mime_type
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT image_data, mime_type, image_path FROM meal_images WHERE id = ?",
            (image_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "image_path": row["image_path"],
        "image_data": bytes(row["image_data"]) if row["image_data"] else None,
        "mime_type": row["mime_type"],
    }


def get_images_without_path(limit: int = 100) -> list[dict]:
    """image_path が NULL で image_data がある画像レコードを返す（BLOB→ファイル移行用）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, meal_id, image_data, mime_type
            FROM meal_images
            WHERE image_path IS NULL AND image_data IS NOT NULL
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "meal_id": r["meal_id"],
            "image_data": bytes(r["image_data"]),
            "mime_type": r["mime_type"],
        }
        for r in rows
    ]


def update_meal_image_path(image_id: int, image_path: str) -> bool:
    """image_id のレコードに image_path を設定し、image_data BLOB を NULL 化する（マイグレーション用）。
    ファイル移行成功後に呼ぶことで DB サイズを削減する。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE meal_images SET image_path = ?, image_data = NULL WHERE id = ?",
            (image_path, image_id),
        )
    return cur.rowcount > 0


def get_meal_images(meal_id: int) -> list[dict]:
    """食事に紐づく全画像を [{id, mime_type, image_path}, ...] で返す"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, mime_type, image_path FROM meal_images WHERE meal_id = ? ORDER BY id",
            (meal_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_meal_image(image_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM meal_images WHERE id = ?", (image_id,))
    return cur.rowcount > 0


def search_meals(query: str, limit: int = 50) -> list[dict]:
    """食事記録をキーワード検索（description・notes の部分一致）"""
    pattern = "%" + query + "%"
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, meal_date, meal_type, description,
                   calories, protein, fat, carbs, sodium, notes
            FROM meals
            WHERE description LIKE ? OR notes LIKE ?
            ORDER BY meal_date DESC, id DESC
            LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── 食事スキップ記録 ───────────────────────────────────────────────────────────

SKIP_MEAL_TYPES = {"breakfast", "lunch", "dinner"}


def save_meal_skip(meal_date: str, meal_type: str) -> None:
    """スキップ記録を保存（既存は IGNORE）。例外は呼び出し元に伝播させる。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO meal_skips (meal_date, meal_type) VALUES (?, ?)",
            (meal_date, meal_type),
        )
        # INSERT OR IGNORE が UNIQUE制約違反を正常処理するため try-except 不要。
        # DB接続エラー等は呼び出し元（main.py）の except Exception + logger.error に伝播させる。


def delete_meal_skip(meal_date: str, meal_type: str) -> bool:
    """スキップ記録を削除。削除件数>0 なら True。"""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM meal_skips WHERE meal_date = ? AND meal_type = ?",
            (meal_date, meal_type),
        )
    return cur.rowcount > 0


def get_meal_skips_by_date(meal_date: str) -> list[str]:
    """指定日のスキップ済み食事タイプ一覧を返す。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT meal_type FROM meal_skips WHERE meal_date = ?",
            (meal_date,),
        ).fetchall()
    return [r["meal_type"] for r in rows]


def get_history(
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """指定期間の記録を日付降順で返す"""
    today = _date.fromisoformat(get_logical_today_jst())
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
            "SELECT id, log_date, time_of_day, weight_kg FROM weight_logs WHERE log_date >= ? AND log_date <= ? ORDER BY log_date DESC",
            (since, until),
        ).fetchall()
        steps_rows = conn.execute(
            "SELECT id, log_date, steps FROM steps_logs WHERE log_date >= ? AND log_date <= ? ORDER BY log_date DESC",
            (since, until),
        ).fetchall()
        skip_rows = conn.execute(
            "SELECT meal_date, meal_type FROM meal_skips "
            "WHERE meal_date >= ? AND meal_date <= ? ORDER BY meal_date",
            (since, until),
        ).fetchall()

    from collections import defaultdict
    days_map: dict = defaultdict(
        lambda: {"meals": [], "weight": {}, "steps": None, "steps_id": None, "skipped_meal_types": []}
    )
    for m in meals:
        days_map[m["meal_date"]]["meals"].append(dict(m))
    for w in weights:
        days_map[w["log_date"]]["weight"][w["time_of_day"]] = {"id": w["id"], "weight_kg": w["weight_kg"]}
    for s in steps_rows:
        days_map[s["log_date"]]["steps"] = s["steps"]
        days_map[s["log_date"]]["steps_id"] = s["id"]
    for sk in skip_rows:
        days_map[sk["meal_date"]]["skipped_meal_types"].append(sk["meal_type"])

    result = []
    for date in sorted(days_map.keys(), reverse=True):
        d = days_map[date]
        ml = d["meals"]
        result.append({
            "date": date,
            "meals": ml,
            "weight": d["weight"],
            "steps": d["steps"],
            "steps_id": d["steps_id"],
            "skipped_meal_types": d["skipped_meal_types"],
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
    today = _date.fromisoformat(get_logical_today_jst())
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
        skip_stat_rows = conn.execute(
            "SELECT meal_date, meal_type FROM meal_skips "
            "WHERE meal_date >= ? AND meal_date <= ?",
            (since, until),
        ).fetchall()

    cal_map = {r["meal_date"]: r for r in cal_rows}
    w_map: dict = {}
    for r in weight_rows:
        w_map.setdefault(r["log_date"], {})[r["time_of_day"]] = r["weight_kg"]
    s_map = {r["log_date"]: r["steps"] for r in step_rows}
    skip_map: dict = {}
    for r in skip_stat_rows:
        skip_map.setdefault(r["meal_date"], []).append(r["meal_type"])

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
        "meal_skips": skip_map,
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
                   calories, protein, fat, carbs, sodium, meal_time
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
        skip_report_rows = conn.execute(
            "SELECT meal_date, meal_type FROM meal_skips "
            "WHERE meal_date BETWEEN ? AND ?",
            (start_date, end_date),
        ).fetchall()

    meal_map: dict = {}
    for m in meals:
        meal_map.setdefault((m["meal_date"], m["meal_type"]), []).append(dict(m))
    w_map: dict = {}
    for w in weights:
        w_map.setdefault(w["log_date"], {})[w["time_of_day"]] = w["weight_kg"]
    s_map = {r["log_date"]: r["steps"] for r in steps_rows}
    skip_map_report: dict = {}
    for r in skip_report_rows:
        skip_map_report.setdefault(r["meal_date"], set()).add(r["meal_type"])

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
            "skipped": {mt: (mt in skip_map_report.get(d, set())) for mt in MEAL_TYPES},
        })

    return {
        "start": start_date,
        "end": end_date,
        "days": days,
        "user_name":    get_setting("user_name") or "—",
        "height_cm":    get_setting("user_height_cm") or "—",
        "calorie_goal": int(get_setting("daily_calorie_goal") or 1500),
        "steps_goal":   int(get_setting("daily_steps_goal") or 8000),
    }


def get_report_data_previous_week(start_date: str) -> dict | None:
    """前週のサマリーデータを返す（AIコメント用）。データなしならNone。"""
    start = _date.fromisoformat(start_date)
    prev_start = (start - timedelta(days=7)).isoformat()
    prev_end = (start - timedelta(days=1)).isoformat()

    with get_conn() as conn:
        meals = conn.execute(
            """
            SELECT meal_date, calories, protein, fat, carbs, sodium
            FROM meals WHERE meal_date BETWEEN ? AND ?
            """,
            (prev_start, prev_end),
        ).fetchall()
        weights = conn.execute(
            "SELECT log_date, time_of_day, weight_kg FROM weight_logs "
            "WHERE log_date BETWEEN ? AND ? ORDER BY log_date",
            (prev_start, prev_end),
        ).fetchall()
        steps_rows = conn.execute(
            "SELECT log_date, steps FROM steps_logs WHERE log_date BETWEEN ? AND ?",
            (prev_start, prev_end),
        ).fetchall()

    if not meals and not weights and not steps_rows:
        return None

    day_cals: dict[str, float] = {}
    day_p: dict[str, float] = {}
    day_f: dict[str, float] = {}
    day_c: dict[str, float] = {}
    day_sod: dict[str, float] = {}
    for m in meals:
        d = m["meal_date"]
        day_cals[d] = day_cals.get(d, 0) + (m["calories"] or 0)
        day_p[d] = day_p.get(d, 0) + (m["protein"] or 0)
        day_f[d] = day_f.get(d, 0) + (m["fat"] or 0)
        day_c[d] = day_c.get(d, 0) + (m["carbs"] or 0)
        day_sod[d] = day_sod.get(d, 0) + (m["sodium"] or 0)

    def _avg(vals: list) -> float | None:
        return round(sum(vals) / len(vals), 1) if vals else None

    cal_vals = list(day_cals.values())
    morning_w = [w["weight_kg"] for w in weights if w["time_of_day"] == "morning"]
    steps_vals = [r["steps"] for r in steps_rows if r["steps"] is not None]

    all_dates = set(m["meal_date"] for m in meals)
    all_dates |= set(w["log_date"] for w in weights)
    all_dates |= set(r["log_date"] for r in steps_rows if r["steps"] is not None)

    return {
        "period": f"{prev_start} ~ {prev_end}",
        "days_count": len(all_dates),
        "avg_calories": _avg(cal_vals),
        "avg_protein": _avg(list(day_p.values())),
        "avg_fat": _avg(list(day_f.values())),
        "avg_carbs": _avg(list(day_c.values())),
        "avg_sodium": _avg(list(day_sod.values())),
        "avg_steps": _avg(steps_vals),
        "weight_start": morning_w[0] if morning_w else None,
        "weight_end": morning_w[-1] if morning_w else None,
    }


def get_daily_summary(target_date: Optional[str] = None) -> dict:
    target_date = target_date or get_logical_today_jst()
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
        skip_rows = conn.execute(
            "SELECT meal_type FROM meal_skips WHERE meal_date = ?",
            (target_date,),
        ).fetchall()

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
        "skipped_meal_types": [r["meal_type"] for r in skip_rows],
        "totals": {
            "calories": total_cal,
            "protein": round(total_p, 1),
            "fat": round(total_f, 1),
            "carbs": round(total_c, 1),
            "sodium": round(total_s, 1),
        },
    }


# ── BMI・基礎代謝計算 ──────────────────────────────────────────────────────────

def calculate_bmi(weight_kg: float, height_cm: float) -> Optional[float]:
    """BMIを計算して返す。身長・体重が0以下の場合はNone。"""
    if height_cm <= 0 or weight_kg <= 0:
        return None
    height_m = height_cm / 100
    return round(weight_kg / (height_m ** 2), 1)


def get_bmi_status(bmi: float) -> str:
    """BMI値に対応するステータス文字列を返す（日本肥満学会基準）。"""
    if bmi < 18.5:
        return "低体重"
    if bmi < 25.0:
        return "普通体重"
    if bmi < 30.0:
        return "肥満(1度)"
    if bmi < 35.0:
        return "肥満(2度)"
    if bmi < 40.0:
        return "肥満(3度)"
    return "肥満(4度)"


def _calc_age_from_birthdate(birthdate_str: str) -> Optional[int]:
    """YYYY-MM-DD文字列から現在の年齢（整数）を返す。不正な場合はNone。"""
    try:
        bd = datetime.strptime(birthdate_str, "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        logger.warning("誕生日のパース失敗: %r", birthdate_str)
        return None
    today = _date.today()
    age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    return age if 1 <= age <= 120 else None


def calculate_bmr(
    weight_kg: float,
    height_cm: float,
    age: Optional[int] = None,
    gender: Optional[str] = None,
) -> dict:
    """Harris-Benedict式（改訂版）で推定基礎代謝を計算する。
    age: 整数年齢（None または範囲外の場合は 40 でフォールバック）
    gender: "male" | "female"（None または不正値の場合は男性でフォールバック）
    戻り値: {"bmr_kcal": int, "bmr_note": str}
    """
    if weight_kg <= 0 or height_cm <= 0:
        return {"bmr_kcal": None, "bmr_note": "計算不可（値が不正）"}

    is_fallback = (
        age is None or not (1 <= age <= 120)
        or gender not in ("male", "female")
    )
    effective_age = age if (age is not None and 1 <= age <= 120) else 40
    effective_gender = gender if gender in ("male", "female") else "male"

    if effective_gender == "male":
        bmr = 88.362 + (13.397 * weight_kg) + (4.799 * height_cm) - (5.677 * effective_age)
        gender_label = "男性"
    else:
        bmr = 447.593 + (9.247 * weight_kg) + (3.098 * height_cm) - (4.330 * effective_age)
        gender_label = "女性"

    if is_fallback:
        note = "推定値（40歳男性基準・性別/年齢未設定）"
    else:
        age_label = str(effective_age)
        note = "推定値（" + age_label + "歳・" + gender_label + "）"

    return {"bmr_kcal": round(bmr), "bmr_note": note}


def get_latest_bmi_info() -> Optional[dict]:
    """最新の体重記録からBMI・基礎代謝情報を返す。
    身長がapp_settingsに設定されていない場合はNone。
    戻り値: {"weight_kg", "height_cm", "bmi", "bmi_status", "bmr_kcal", "bmr_note", "log_date"}
    """
    height_str = get_setting("user_height_cm")
    if not height_str:
        return None
    try:
        height_cm = float(height_str)
    except ValueError:
        return None
    if height_cm <= 0:
        return None

    with get_conn() as conn:
        row = conn.execute(
            "SELECT weight_kg, log_date FROM weight_logs ORDER BY log_date DESC, id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None

    weight_kg = row["weight_kg"]
    bmi = calculate_bmi(weight_kg, height_cm)
    gender = get_setting("user_gender") or ""
    birthdate_str = get_setting("user_birthdate") or ""
    age = _calc_age_from_birthdate(birthdate_str) if birthdate_str else None
    bmr_info = calculate_bmr(weight_kg, height_cm, age=age, gender=gender or None)
    return {
        "weight_kg": weight_kg,
        "height_cm": height_cm,
        "bmi": bmi,
        "bmi_status": get_bmi_status(bmi) if bmi is not None else None,
        "bmr_kcal": bmr_info["bmr_kcal"],
        "bmr_note": bmr_info["bmr_note"],
        "log_date": row["log_date"],
    }


# ── 睡眠ログ ───────────────────────────────────────────────────────────────────

def _calc_sleep_duration(sleep_start: str, sleep_end: str) -> Optional[int]:
    """HH:MM形式の開始・終了から睡眠時間（分）を計算する。
    日付跨ぎ（例: 23:00→07:00）に対応。
    パース失敗時はNoneを返す。
    """
    try:
        sh, sm = map(int, sleep_start.split(":"))
        eh, em = map(int, sleep_end.split(":"))
    except (ValueError, AttributeError):
        return None
    start_total = sh * 60 + sm
    end_total = eh * 60 + em
    if end_total <= start_total:
        end_total += 24 * 60
    return end_total - start_total


def upsert_sleep_log(
    date: str,
    sleep_start: str,
    sleep_end: str,
    deep_minutes: Optional[int] = None,
    rem_minutes: Optional[int] = None,
    awake_minutes: Optional[int] = None,
    source: str = "healthkit",
) -> dict:
    """睡眠ログを登録（同日は上書き）。戻り値: {"id": int, "updated": bool, "duration_minutes": int}"""
    duration = _calc_sleep_duration(sleep_start, sleep_end)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM sleep_logs WHERE date = ?", (date,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE sleep_logs SET
                    sleep_start=?, sleep_end=?, duration_minutes=?,
                    deep_minutes=?, rem_minutes=?, awake_minutes=?,
                    source=?, recorded_at=datetime('now','localtime')
                WHERE date=?
                """,
                (sleep_start, sleep_end, duration, deep_minutes, rem_minutes,
                 awake_minutes, source, date),
            )
            return {"id": existing["id"], "updated": True, "duration_minutes": duration}
        else:
            cur = conn.execute(
                """
                INSERT INTO sleep_logs
                    (date, sleep_start, sleep_end, duration_minutes,
                     deep_minutes, rem_minutes, awake_minutes, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (date, sleep_start, sleep_end, duration,
                 deep_minutes, rem_minutes, awake_minutes, source),
            )
            return {"id": cur.lastrowid, "updated": False, "duration_minutes": duration}


def get_sleep_logs(start_date: str, end_date: str) -> list[dict]:
    """指定期間の睡眠ログを日付昇順で返す。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, date, sleep_start, sleep_end, duration_minutes,
                   deep_minutes, rem_minutes, awake_minutes, source
            FROM sleep_logs
            WHERE date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (start_date, end_date),
        ).fetchall()
    return [dict(r) for r in rows]


# ── バイタルログ ───────────────────────────────────────────────────────────────

def insert_vital_log(
    date: str,
    vital_type: str,
    value: Optional[float] = None,
    time: Optional[str] = None,
    note: Optional[str] = None,
    source: str = "healthkit",
) -> int:
    """バイタルログを1件挿入する。戻り値: 挿入したID。
    vital_type は 'heart_rate' / 'spo2' / 'bp_alert' のいずれか。
    """
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO vitals_logs (date, time, type, value, note, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (date, time, vital_type, value, note, source),
        )
        return cur.lastrowid


def get_vital_logs(
    start_date: str,
    end_date: str,
    vital_type: Optional[str] = None,
) -> list[dict]:
    """指定期間のバイタルログを返す。vital_type指定で絞り込み可能。"""
    with get_conn() as conn:
        if vital_type:
            rows = conn.execute(
                """
                SELECT id, date, time, type, value, note, source
                FROM vitals_logs
                WHERE date >= ? AND date <= ? AND type = ?
                ORDER BY date ASC, id ASC
                """,
                (start_date, end_date, vital_type),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, date, time, type, value, note, source
                FROM vitals_logs
                WHERE date >= ? AND date <= ?
                ORDER BY date ASC, id ASC
                """,
                (start_date, end_date),
            ).fetchall()
    return [dict(r) for r in rows]

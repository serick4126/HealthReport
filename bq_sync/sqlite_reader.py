"""
bq_sync/sqlite_reader.py - SQLite読取リーダー
WALチェックポイント・各テーブル抽出・日付範囲・sync_change_log参照・daily_summary集計
"""
import json
import sqlite3
from datetime import datetime, timedelta, date
from configparser import ConfigParser


def checkpoint_wal(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def resolve_date_range(
    config: ConfigParser,
    full: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
) -> tuple[str, str, str | None]:
    if from_date is not None and to_date is not None:
        return (from_date, to_date, None)

    if full:
        from_date = config["sync"]["initial_from_date"]
        to_date = date.today().isoformat()
        return (from_date, to_date, None)

    lookback_days = config["sync"].getint("lookback_days")
    now = datetime.now()
    today = now.date().isoformat()
    from_date = (now.date() - timedelta(days=lookback_days)).isoformat()
    cutoff = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    return (from_date, today, cutoff)


def collect_changed_dates(db_path: str, cutoff: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT log_date FROM sync_change_log WHERE changed_at >= ?",
            (cutoff,),
        ).fetchall()
    return {r["log_date"] for r in rows}


def _extract_table(
    db_path: str,
    table: str,
    date_col: str,
    columns: str,
    dates: set[str] | list[str],
) -> list[dict]:
    if not dates:
        return []
    dates_list = sorted(dates)
    placeholders = ",".join(["?"] * len(dates_list))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sql = (
            f"SELECT {columns} FROM {table} "
            f"WHERE {date_col} IN ({placeholders}) ORDER BY {date_col}"
        )
        rows = conn.execute(sql, dates_list).fetchall()
    return [dict(r) for r in rows]


def extract_weight_logs(db_path: str, dates: set[str] | list[str]) -> list[dict]:
    return _extract_table(
        db_path, "weight_logs", "log_date",
        "id, log_date, time_of_day, weight_kg, recorded_at",
        dates,
    )


def extract_blood_pressure_logs(db_path: str, dates: set[str] | list[str]) -> list[dict]:
    return _extract_table(
        db_path, "blood_pressure_logs", "log_date",
        "id, log_date, time_of_day, systolic, diastolic, recorded_at",
        dates,
    )


def extract_exercise_logs(db_path: str, dates: set[str] | list[str]) -> list[dict]:
    return _extract_table(
        db_path, "exercise_logs", "log_date",
        "id, log_date, calories_burned, description, source, recorded_at",
        dates,
    )


def extract_steps_logs(db_path: str, dates: set[str] | list[str]) -> list[dict]:
    return _extract_table(
        db_path, "steps_logs", "log_date",
        "id, log_date, steps, recorded_at",
        dates,
    )


def extract_body_fat_logs(db_path: str, dates: set[str] | list[str]) -> list[dict]:
    return _extract_table(
        db_path, "body_fat_logs", "log_date",
        "id, log_date, body_fat_pct, recorded_at",
        dates,
    )


def extract_meals(db_path: str, dates: set[str] | list[str]) -> list[dict]:
    if not dates:
        return []
    dates_list = sorted(dates)
    placeholders = ",".join(["?"] * len(dates_list))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sql = (
            "SELECT id, meal_date AS log_date, meal_type, meal_time, description, "
            "calories, protein, fat, carbs, sodium, notes "
            "FROM meals WHERE meal_date IN ({placeholders}) "
            "ORDER BY meal_date, meal_type"
        ).replace("{placeholders}", placeholders)
        rows = conn.execute(sql, dates_list).fetchall()
    return [dict(r) for r in rows]


_DAILY_SUMMARY_SQL = """
SELECT
    d.log_date,
    CASE CAST(strftime('%w', d.log_date) AS INTEGER)
        WHEN 0 THEN '日' WHEN 1 THEN '月' WHEN 2 THEN '火'
        WHEN 3 THEN '水' WHEN 4 THEN '木' WHEN 5 THEN '金'
        WHEN 6 THEN '土'
    END AS weekday,
    m.calories_total,
    m.protein_g,
    m.fat_g,
    m.carbs_g,
    m.sodium_g,
    w.weight_morning_kg,
    w.weight_evening_kg,
    bf.body_fat_pct,
    sl.steps,
    e.exercise_calories,
    bp.bp_morning_systolic,
    bp.bp_morning_diastolic,
    bp.bp_evening_systolic,
    bp.bp_evening_diastolic,
    weather.weather,
    memo.memo_text AS memo
FROM (
    SELECT DISTINCT log_date FROM (
        SELECT meal_date AS log_date FROM meals WHERE meal_date IN ({placeholders})
        UNION SELECT log_date FROM weight_logs WHERE log_date IN ({placeholders})
        UNION SELECT log_date FROM blood_pressure_logs WHERE log_date IN ({placeholders})
        UNION SELECT log_date FROM steps_logs WHERE log_date IN ({placeholders})
        UNION SELECT value AS log_date FROM json_each(?)
    )
) d
LEFT JOIN (
    SELECT meal_date AS log_date,
        SUM(calories)         AS calories_total,
        ROUND(SUM(protein),1) AS protein_g,
        ROUND(SUM(fat),1)     AS fat_g,
        ROUND(SUM(carbs),1)   AS carbs_g,
        ROUND(SUM(sodium),2)  AS sodium_g
    FROM meals WHERE meal_date IN ({placeholders})
    GROUP BY meal_date
) m ON m.log_date = d.log_date
LEFT JOIN (
    SELECT log_date,
        MAX(CASE WHEN time_of_day = 'morning' THEN weight_kg END) AS weight_morning_kg,
        MAX(CASE WHEN time_of_day = 'evening' THEN weight_kg END) AS weight_evening_kg
    FROM weight_logs WHERE log_date IN ({placeholders})
    GROUP BY log_date
) w ON w.log_date = d.log_date
LEFT JOIN (
    SELECT log_date, MAX(body_fat_pct) AS body_fat_pct
    FROM body_fat_logs WHERE log_date IN ({placeholders})
    GROUP BY log_date
) bf ON bf.log_date = d.log_date
LEFT JOIN (
    SELECT log_date, MAX(steps) AS steps
    FROM steps_logs WHERE log_date IN ({placeholders})
    GROUP BY log_date
) sl ON sl.log_date = d.log_date
LEFT JOIN (
    SELECT log_date, SUM(calories_burned) AS exercise_calories
    FROM exercise_logs WHERE log_date IN ({placeholders})
    GROUP BY log_date
) e ON e.log_date = d.log_date
LEFT JOIN (
    SELECT log_date,
        MAX(CASE WHEN time_of_day = 'morning' THEN systolic  END) AS bp_morning_systolic,
        MAX(CASE WHEN time_of_day = 'morning' THEN diastolic END) AS bp_morning_diastolic,
        MAX(CASE WHEN time_of_day = 'evening' THEN systolic  END) AS bp_evening_systolic,
        MAX(CASE WHEN time_of_day = 'evening' THEN diastolic END) AS bp_evening_diastolic
    FROM blood_pressure_logs WHERE log_date IN ({placeholders})
    GROUP BY log_date
) bp ON bp.log_date = d.log_date
LEFT JOIN (
    SELECT log_date, MAX(weather) AS weather
    FROM weather_logs WHERE log_date IN ({placeholders})
    GROUP BY log_date
) weather ON weather.log_date = d.log_date
LEFT JOIN (
    SELECT log_date, memo_text
    FROM memos WHERE log_date IN ({placeholders})
) memo ON memo.log_date = d.log_date
ORDER BY d.log_date
"""


def aggregate_daily_summary(
    db_path: str,
    dates: set[str] | list[str],
    calories_goal: int | None,
) -> list[dict]:
    if not dates:
        return []
    dates_list = sorted(dates)
    placeholders = ",".join(["?"] * len(dates_list))
    dates_json = json.dumps(dates_list)
    # driving: meals/weight/bp/steps の4箇所 + json_each
    # joins: meals/weight/body_fat/steps/exercise/bp/weather/memos の8箇所
    params = dates_list * 4 + [dates_json] + dates_list * 8

    sql = _DAILY_SUMMARY_SQL.replace("{placeholders}", placeholders)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        cal = d.get("calories_total")
        if cal is not None:
            d["calories_total"] = int(cal)
        if d.get("exercise_calories") is not None:
            d["exercise_calories"] = int(d["exercise_calories"])
        if d.get("steps") is not None:
            d["steps"] = int(d["steps"])
        d["calories_goal"] = calories_goal
        if cal is not None and calories_goal is not None:
            d["calories_balance"] = int(cal) - calories_goal
        else:
            d["calories_balance"] = None
        results.append(d)

    return results

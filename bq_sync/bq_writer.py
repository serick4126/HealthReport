import logging
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery
from google.cloud.bigquery import LoadJobConfig, WriteDisposition

logger = logging.getLogger(__name__)

_TMP_TABLE_PREFIX = "_tmp_"
_GOAL_KEY_CALORIES = "calories_goal"

SCHEMA_WEIGHT_LOGS = [
    bigquery.SchemaField("id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("log_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("time_of_day", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("weight_kg", "FLOAT64", mode="REQUIRED"),
    bigquery.SchemaField("recorded_at", "TIMESTAMP"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]

SCHEMA_BLOOD_PRESSURE_LOGS = [
    bigquery.SchemaField("id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("log_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("time_of_day", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("systolic", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("diastolic", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("recorded_at", "TIMESTAMP"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]

SCHEMA_EXERCISE_LOGS = [
    bigquery.SchemaField("id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("log_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("description", "STRING"),
    bigquery.SchemaField("calories_burned", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("source", "STRING"),
    bigquery.SchemaField("recorded_at", "TIMESTAMP"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]

SCHEMA_STEPS_LOGS = [
    bigquery.SchemaField("id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("log_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("steps", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("recorded_at", "TIMESTAMP"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]

SCHEMA_BODY_FAT_LOGS = [
    bigquery.SchemaField("id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("log_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("body_fat_pct", "FLOAT64", mode="REQUIRED"),
    bigquery.SchemaField("recorded_at", "TIMESTAMP"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]

SCHEMA_MEALS = [
    bigquery.SchemaField("id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("log_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("meal_type", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("meal_time", "STRING"),
    bigquery.SchemaField("description", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("calories", "INT64"),
    bigquery.SchemaField("protein", "FLOAT64"),
    bigquery.SchemaField("fat", "FLOAT64"),
    bigquery.SchemaField("carbs", "FLOAT64"),
    bigquery.SchemaField("sodium", "FLOAT64"),
    bigquery.SchemaField("notes", "STRING"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]

SCHEMA_DAILY_SUMMARY = [
    bigquery.SchemaField("log_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("weekday", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("calories_total", "INT64"),
    bigquery.SchemaField("calories_goal", "INT64"),
    bigquery.SchemaField("calories_balance", "INT64"),
    bigquery.SchemaField("protein_g", "FLOAT64"),
    bigquery.SchemaField("fat_g", "FLOAT64"),
    bigquery.SchemaField("carbs_g", "FLOAT64"),
    bigquery.SchemaField("sodium_g", "FLOAT64"),
    bigquery.SchemaField("weight_morning_kg", "FLOAT64"),
    bigquery.SchemaField("weight_evening_kg", "FLOAT64"),
    bigquery.SchemaField("body_fat_pct", "FLOAT64"),
    bigquery.SchemaField("steps", "INT64"),
    bigquery.SchemaField("exercise_calories", "INT64"),
    bigquery.SchemaField("bp_morning_systolic", "INT64"),
    bigquery.SchemaField("bp_morning_diastolic", "INT64"),
    bigquery.SchemaField("bp_evening_systolic", "INT64"),
    bigquery.SchemaField("bp_evening_diastolic", "INT64"),
    bigquery.SchemaField("weather", "STRING"),
    bigquery.SchemaField("memo", "STRING"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]

SCHEMA_GOAL_CHANGE_LOGS = [
    bigquery.SchemaField("id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("log_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("key", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("old_value", "STRING"),
    bigquery.SchemaField("new_value", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("detected_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("synced_at", "TIMESTAMP", mode="REQUIRED"),
]


def create_bq_client() -> bigquery.Client:
    key_path = Path(__file__).parent / "service_account_writer.json"
    if not key_path.exists():
        raise FileNotFoundError(
            f"SA-1キーファイルが見つかりません: {key_path}"
        )
    return bigquery.Client.from_service_account_json(str(key_path))


def upsert_table(
    client: bigquery.Client,
    project_id: str,
    dataset_id: str,
    table_name: str,
    rows: list[dict],
    schema: list[bigquery.SchemaField],
    merge_key: str,
) -> None:
    if not rows:
        return
    synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{**r, "synced_at": synced_at} for r in rows]
    tmp_table_name = f"{_TMP_TABLE_PREFIX}{table_name}"
    tmp_table_ref = f"{project_id}.{dataset_id}.{tmp_table_name}"
    table_ref = f"{project_id}.{dataset_id}.{table_name}"

    col_names = [f.name for f in schema]
    col_names_quoted = [f"`{c}`" for c in col_names]
    non_key_cols = [c for c in col_names if c != merge_key]
    update_clause = ", ".join(f"T.`{c}` = S.`{c}`" for c in non_key_cols)
    insert_cols = ", ".join(col_names_quoted)
    insert_values = ", ".join(f"S.`{c}`" for c in col_names)

    create_sql = (
        f"CREATE OR REPLACE TABLE `{tmp_table_ref}` AS "
        f"SELECT * FROM `{table_ref}` WHERE FALSE"
    )
    merge_sql = (
        f"MERGE `{table_ref}` T "
        f"USING `{tmp_table_ref}` S "
        f"ON T.`{merge_key}` = S.`{merge_key}` "
        f"WHEN MATCHED THEN UPDATE SET {update_clause} "
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) "
        f"VALUES ({insert_values})"
    )

    client.query(create_sql).result()
    try:
        client.load_table_from_json(
            rows,
            tmp_table_ref,
            job_config=LoadJobConfig(
                write_disposition=WriteDisposition.WRITE_TRUNCATE,
            ),
        ).result()
        client.query(merge_sql).result()
    finally:
        client.delete_table(tmp_table_ref)


def delete_and_insert_daily_summary(
    client: bigquery.Client,
    project_id: str,
    dataset_id: str,
    rows: list[dict],
    dates: list[str],
) -> None:
    if not rows or not dates:
        return
    table_ref = f"{project_id}.{dataset_id}.daily_summary"
    synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{**r, "synced_at": synced_at} for r in rows]

    quoted_dates = ", ".join(f"'{d}'" for d in dates)
    delete_sql = (
        f"DELETE FROM `{table_ref}` WHERE log_date IN ({quoted_dates})"
    )
    client.query(delete_sql).result()

    client.load_table_from_json(
        rows,
        table_ref,
        job_config=LoadJobConfig(
            write_disposition=WriteDisposition.WRITE_APPEND,
        ),
    ).result()


def cleanup_temp_tables(
    client: bigquery.Client,
    project_id: str,
    dataset_id: str,
) -> None:
    dataset_ref = f"{project_id}.{dataset_id}"
    for table in client.list_tables(dataset_ref):
        if table.table_id.startswith(_TMP_TABLE_PREFIX):
            try:
                client.delete_table(table.reference)
                logger.info("残留一時テーブルを削除: %s", table.table_id)
            except Exception as e:
                logger.warning("一時テーブル削除失敗 (%s): %s", table.table_id, e)


def get_current_calories_goal_from_bq(
    client: bigquery.Client,
    project_id: str,
    dataset_id: str,
) -> str | None:
    sql = (
        f"SELECT new_value FROM `{project_id}.{dataset_id}.goal_change_logs` "
        f"WHERE key = '{_GOAL_KEY_CALORIES}' "
        f"ORDER BY detected_at DESC LIMIT 1"
    )
    rows = list(client.query(sql).result())
    if not rows:
        return None
    return rows[0].new_value


def upsert_goal_change_log(
    client: bigquery.Client,
    project_id: str,
    dataset_id: str,
    old_value: str | None,
    new_value: str,
    today: str,
) -> None:
    table_ref = f"{project_id}.{dataset_id}.goal_change_logs"

    max_id_sql = f"SELECT MAX(id) AS max_id FROM `{table_ref}`"
    max_id_row = list(client.query(max_id_sql).result())[0]
    next_id = 1 if max_id_row.max_id is None else int(max_id_row.max_id) + 1

    insert_sql = (
        f"INSERT INTO `{table_ref}` "
        f"(id, log_date, `key`, old_value, new_value, detected_at, synced_at) "
        f"VALUES ("
        f"{next_id}, "
        f"DATE('{today}'), "
        f"'{_GOAL_KEY_CALORIES}', "
        f"{'NULL' if old_value is None else f'\"{old_value}\"'}, "
        f"'{new_value}', "
        f"CURRENT_TIMESTAMP(), "
        f"CURRENT_TIMESTAMP()"
        f")"
    )
    client.query(insert_sql).result()

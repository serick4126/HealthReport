"""
bq_sync/sync.py - BigQuery同期オーケストレーター
CLI引数解析・同期フロー全体制御・エラーハンドリング・ログ出力
"""
import argparse
import logging
import sys
import sqlite3
import time
from configparser import ConfigParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date as _date

from bq_sync.config import load_config
from bq_sync.sqlite_reader import (
    checkpoint_wal,
    resolve_date_range,
    collect_changed_dates,
    extract_weight_logs,
    extract_blood_pressure_logs,
    extract_exercise_logs,
    extract_steps_logs,
    extract_body_fat_logs,
    extract_meals,
    aggregate_daily_summary,
)
from bq_sync.bq_writer import (
    create_bq_client,
    upsert_table,
    delete_and_insert_daily_summary,
    cleanup_temp_tables,
    get_current_calories_goal_from_bq,
    upsert_goal_change_log,
    SCHEMA_WEIGHT_LOGS,
    SCHEMA_BLOOD_PRESSURE_LOGS,
    SCHEMA_EXERCISE_LOGS,
    SCHEMA_STEPS_LOGS,
    SCHEMA_BODY_FAT_LOGS,
    SCHEMA_MEALS,
    SCHEMA_DAILY_SUMMARY,
)

logger = logging.getLogger(__name__)

_SYNC_TASK_DEFS = [
    ("weight_logs", "extract_weight_logs", SCHEMA_WEIGHT_LOGS, "id"),
    ("blood_pressure_logs", "extract_blood_pressure_logs", SCHEMA_BLOOD_PRESSURE_LOGS, "id"),
    ("exercise_logs", "extract_exercise_logs", SCHEMA_EXERCISE_LOGS, "id"),
    ("steps_logs", "extract_steps_logs", SCHEMA_STEPS_LOGS, "id"),
    ("body_fat_logs", "extract_body_fat_logs", SCHEMA_BODY_FAT_LOGS, "id"),
    ("meals", "extract_meals", SCHEMA_MEALS, "id"),
]

_MODULE = sys.modules[__name__]


_CLEANUP_RETENTION_DAYS = 30


def _resolve_extract_fn(name: str):
    return getattr(_MODULE, name)


def _get_setting_value(db_path: str, key: str) -> str | None:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        logger.error("app_settings 読み取りエラー (key=%s): %s", key, e)
    return None


def _get_day_start_hour(db_path: str) -> int:
    value = _get_setting_value(db_path, "day_start_hour")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            pass
    return 4


def _get_logical_today(db_path: str) -> str:
    hour = _get_day_start_hour(db_path)
    now = datetime.now()
    if now.hour < hour:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def _ensure_date_range(
    from_date: str,
    to_date: str,
) -> set[str]:
    dates = set()
    try:
        start = _date.fromisoformat(from_date)
        end = _date.fromisoformat(to_date)
        current = start
        while current <= end:
            dates.add(current.isoformat())
            current += timedelta(days=1)
    except ValueError:
        logger.warning("日付範囲の解析に失敗しました: %s 〜 %s", from_date, to_date)
    return dates


def _sync_detail_tables(
    client,
    project_id: str,
    dataset_id: str,
    db_path: str,
    dates: set[str],
) -> tuple[int, int]:
    detail_synced = 0
    detail_rows = 0
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for table_name, extract_fn_name, schema, merge_key in _SYNC_TASK_DEFS:
            extract_fn = _resolve_extract_fn(extract_fn_name)
            future = executor.submit(
                _sync_one_table,
                client, project_id, dataset_id, db_path,
                table_name, extract_fn, schema, merge_key, dates,
            )
            futures[future] = table_name

        for future in as_completed(futures):
            table_name = futures[future]
            try:
                result = future.result()
                if result is not None:
                    detail_synced += 1
                    detail_rows += result
            except Exception as e:
                logger.error("テーブル %s の同期中に予期せぬエラーが発生しました: %s", table_name, e)

    return detail_synced, detail_rows


def _sync_one_table(
    client,
    project_id: str,
    dataset_id: str,
    db_path: str,
    table_name: str,
    extract_fn,
    schema,
    merge_key: str,
    dates: set[str],
) -> int:
    try:
        rows = extract_fn(db_path, dates)
    except Exception as e:
        logger.error("テーブル %s のSQLite読み取りエラー: %s", table_name, e)
        return 0
    try:
        upsert_table(
            client, project_id, dataset_id, table_name, rows, schema, merge_key,
            target_dates=sorted(dates),
        )
        return len(rows)
    except Exception as e:
        logger.error("テーブル %s のBigQuery同期エラー: %s", table_name, e)
        raise


def _sync_goal_change_logs(
    client,
    project_id: str,
    dataset_id: str,
    db_path: str,
    today: str,
) -> None:
    local_goal = _get_setting_value(db_path, "daily_calorie_goal")
    if local_goal is None:
        return

    try:
        bq_goal = get_current_calories_goal_from_bq(client, project_id, dataset_id)
        if bq_goal is not None and bq_goal == local_goal:
            return
        upsert_goal_change_log(client, project_id, dataset_id, bq_goal, local_goal, today)
    except Exception as e:
        logger.error("goal_change_logs 同期エラー: %s", e)


def _cleanup_sync_change_log(db_path: str) -> int:
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "DELETE FROM sync_change_log "
                f"WHERE changed_at < datetime('now','localtime', '-{_CLEANUP_RETENTION_DAYS} days')"
            )
            deleted = cur.rowcount
            conn.commit()
            return deleted
    except Exception as e:
        logger.error("sync_change_log クリーンアップエラー: %s", e)
        return 0


def _log_completion(
    elapsed: float,
    date_count: int,
    detail_synced: int,
    detail_rows: int,
    daily_rows: int,
    log_cleanup_count: int,
) -> None:
    logger.info("=" * 60)
    logger.info("BigQuery 同期完了")
    logger.info("  対象日数: %d 日", date_count)
    logger.info("  明細テーブル同期: %d テーブル (%d 件)", detail_synced, detail_rows)
    logger.info("  日次サマリ同期件数: %d 件", daily_rows)
    logger.info("  sync_change_log クリーンアップ: %d 件", log_cleanup_count)
    logger.info("  所要時間: %.1f 秒", elapsed)
    logger.info("=" * 60)


def run_sync(
    config: ConfigParser,
    db_path: str,
    project_id: str,
    dataset_id: str,
    full: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
) -> None:
    start_time = time.time()

    try:
        client = create_bq_client()
    except FileNotFoundError as e:
        logger.error("BigQuery 認証エラー: %s", e)
        sys.exit(1)
        return

    cleanup_temp_tables(client, project_id, dataset_id)

    checkpoint_wal(db_path)

    from_str, to_str, cutoff = resolve_date_range(
        config, full=full, from_date=from_date, to_date=to_date,
    )

    dates = _ensure_date_range(from_str, to_str)

    if not full and cutoff is not None:
        changed = collect_changed_dates(db_path, cutoff)
        dates |= changed

    if not dates:
        logger.warning("同期対象日付がありません。")
        return

    today = _get_logical_today(db_path)

    _sync_goal_change_logs(client, project_id, dataset_id, db_path, today)

    detail_synced, detail_rows = _sync_detail_tables(client, project_id, dataset_id, db_path, dates)

    calories_goal_str = _get_setting_value(db_path, "daily_calorie_goal")
    calories_goal = int(calories_goal_str) if calories_goal_str is not None else None

    daily_rows = aggregate_daily_summary(db_path, dates, calories_goal)
    if daily_rows:
        sorted_dates = sorted(dates)
        delete_and_insert_daily_summary(client, project_id, dataset_id, daily_rows, sorted_dates)

    log_cleanup_count = _cleanup_sync_change_log(db_path)

    elapsed = time.time() - start_time
    _log_completion(elapsed, len(dates), detail_synced, detail_rows, len(daily_rows), log_cleanup_count)


def main() -> None:
    parser = argparse.ArgumentParser(description="HealthReport BigQuery 同期ツール")
    parser.add_argument("--full", action="store_true", help="初回全件同期")
    parser.add_argument("--from", dest="from_date", help="開始日 (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", help="終了日 (YYYY-MM-DD)")
    parser.add_argument("--config", default=None, help="設定ファイルのパス")
    parser.add_argument("--log-level", default="INFO", help="ログレベル")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.config)
    db_path = config["sqlite"]["db_path"]
    project_id = config["bigquery"]["project_id"]
    dataset_id = config["bigquery"]["dataset_id"]

    if args.from_date and not args.to_date:
        parser.error("--from を指定する場合は --to も必須です")
    if args.to_date and not args.from_date:
        parser.error("--to を指定する場合は --from も必須です")
    if args.full and (args.from_date or args.to_date):
        parser.error("--full と --from/--to は同時に指定できません")

    run_sync(
        config,
        db_path,
        project_id,
        dataset_id,
        full=args.full,
        from_date=args.from_date,
        to_date=args.to_date,
    )


if __name__ == "__main__":
    main()

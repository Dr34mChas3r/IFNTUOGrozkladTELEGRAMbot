"""
Одноразовий скрипт міграції: переносить дані з наявних
user_settings.json та schedule_cache_global.json у Heroku Postgres.

Запуск ЛОКАЛЬНО (з тими самими JSON-файлами поруч) або одноразово на
Heroku через:
    heroku run python migrate_to_postgres.py

DATABASE_URL береться автоматично з env (Heroku config vars) або з .env
локально (python-dotenv), як і в bot_global.py.
"""

import os
import json
import logging

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

USER_SETTINGS_FILE = "user_settings.json"
SCHEDULE_CACHE_FILE = "schedule_cache_global.json"


def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL не знайдено в env")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url, sslmode="require")


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id BIGINT PRIMARY KEY,
                group_name TEXT,
                group_id TEXT,
                change_notifications BOOLEAN NOT NULL DEFAULT FALSE,
                daily_notifications BOOLEAN NOT NULL DEFAULT FALSE,
                weekly_notifications BOOLEAN NOT NULL DEFAULT FALSE,
                pinned_messages JSONB NOT NULL DEFAULT '[]'::jsonb
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schedule_cache (
                group_id TEXT PRIMARY KEY,
                events JSONB NOT NULL DEFAULT '[]'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    conn.commit()


def migrate_users(conn):
    if not os.path.exists(USER_SETTINGS_FILE):
        logger.warning(f"{USER_SETTINGS_FILE} не знайдено, пропускаю users")
        return
    with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    users = data.get("users", [])
    with conn.cursor() as cur:
        for u in users:
            cur.execute("""
                INSERT INTO users (chat_id, group_name, group_id,
                    change_notifications, daily_notifications,
                    weekly_notifications, pinned_messages)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE SET
                    group_name = EXCLUDED.group_name,
                    group_id = EXCLUDED.group_id,
                    change_notifications = EXCLUDED.change_notifications,
                    daily_notifications = EXCLUDED.daily_notifications,
                    weekly_notifications = EXCLUDED.weekly_notifications,
                    pinned_messages = EXCLUDED.pinned_messages
            """, (
                u["chat_id"], u.get("group_name"), u.get("group_id"),
                u.get("change_notifications", False),
                u.get("daily_notifications", False),
                u.get("weekly_notifications", False),
                psycopg2.extras.Json(u.get("pinned_messages", []))
            ))
    conn.commit()
    logger.info(f"Перенесено {len(users)} користувачів")


def migrate_schedule_cache(conn):
    if not os.path.exists(SCHEDULE_CACHE_FILE):
        logger.warning(f"{SCHEDULE_CACHE_FILE} не знайдено, пропускаю кеш розкладу")
        return
    with open(SCHEDULE_CACHE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    with conn.cursor() as cur:
        for group_id, events in data.items():
            cur.execute("""
                INSERT INTO schedule_cache (group_id, events, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (group_id) DO UPDATE SET
                    events = EXCLUDED.events, updated_at = now()
            """, (group_id, psycopg2.extras.Json(events)))
    conn.commit()
    logger.info(f"Перенесено кеш розкладу для {len(data)} груп")


def main():
    conn = get_db_connection()
    try:
        ensure_schema(conn)
        migrate_users(conn)
        migrate_schedule_cache(conn)
        logger.info("Міграцію завершено успішно.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

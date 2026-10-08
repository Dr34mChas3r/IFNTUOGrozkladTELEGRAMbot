import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL не знайдено в змінних середовища.")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url, sslmode="require")

class UserSettings:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.group_name: Optional[str] = None
        self.group_id: Optional[str] = None
        self.change_notifications = False
        self.daily_notifications = False
        self.weekly_notifications = False
        self.pinned_messages: List[int] = []
        self.disabled_electives: List[str] = []
        self.debug_mode = False
        self.theme = "light"

    def to_dict(self) -> dict:
        return {
            'chat_id': self.chat_id,
            'group_name': self.group_name,
            'group_id': self.group_id,
            'change_notifications': self.change_notifications,
            'daily_notifications': self.daily_notifications,
            'weekly_notifications': self.weekly_notifications,
            'pinned_messages': self.pinned_messages,
            'disabled_electives': self.disabled_electives,
            'debug_mode': getattr(self, 'debug_mode', False),
            'theme': getattr(self, 'theme', 'light')
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'UserSettings':
        settings = cls(data['chat_id'])
        settings.group_name = data.get('group_name')
        settings.group_id = data.get('group_id')
        settings.change_notifications = data.get('change_notifications', False)
        settings.daily_notifications = data.get('daily_notifications', False)
        settings.weekly_notifications = data.get('weekly_notifications', False)
        settings.pinned_messages = data.get('pinned_messages', [])
        settings.disabled_electives = data.get('disabled_electives', [])
        settings.debug_mode = data.get('debug_mode', False)
        settings.theme = data.get('theme', 'light')
        return settings

    @classmethod
    def _from_row(cls, row: dict) -> 'UserSettings':
        s = cls(row['chat_id'])
        s.group_name = row['group_name']
        s.group_id = row['group_id']
        s.change_notifications = row['change_notifications']
        s.daily_notifications = row['daily_notifications']
        s.weekly_notifications = row['weekly_notifications']
        s.pinned_messages = row['pinned_messages'] or []
        s.disabled_electives = row.get('disabled_electives') or []
        s.debug_mode = row.get('debug_mode', False)
        s.theme = row.get('theme', 'light')
        return s

class UserManager:
    def __init__(self):
        self.users: Dict[int, UserSettings] = {}
        self._ensure_schema()
        self._load_all()

    def _ensure_schema(self):
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        chat_id BIGINT PRIMARY KEY,
                        group_name TEXT,
                        group_id TEXT,
                        change_notifications BOOLEAN NOT NULL DEFAULT FALSE,
                        daily_notifications BOOLEAN NOT NULL DEFAULT FALSE,
                        weekly_notifications BOOLEAN NOT NULL DEFAULT FALSE,
                        pinned_messages JSONB NOT NULL DEFAULT '[]'::jsonb,
                        disabled_electives JSONB NOT NULL DEFAULT '[]'::jsonb,
                        debug_mode BOOLEAN NOT NULL DEFAULT FALSE
                    )
                """)
                # Безпечне додавання нової колонки, якщо вона ще не існує
                cur.execute("""
                    ALTER TABLE users 
                    ADD COLUMN IF NOT EXISTS theme TEXT NOT NULL DEFAULT 'light';
                """)
            conn.commit()

    def _load_all(self):
        self.users = {}
        try:
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM users")
                    for row in cur.fetchall():
                        s = UserSettings._from_row(row)
                        self.users[s.chat_id] = s
        except Exception as e:
            logger.error(f"Users load error: {e}")

    def _upsert(self, settings: UserSettings):
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users (chat_id, group_name, group_id,
                            change_notifications, daily_notifications,
                            weekly_notifications, pinned_messages,
                            disabled_electives, debug_mode, theme)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (chat_id) DO UPDATE SET
                            group_name = EXCLUDED.group_name,
                            group_id = EXCLUDED.group_id,
                            change_notifications = EXCLUDED.change_notifications,
                            daily_notifications = EXCLUDED.daily_notifications,
                            weekly_notifications = EXCLUDED.weekly_notifications,
                            pinned_messages = EXCLUDED.pinned_messages,
                            disabled_electives = EXCLUDED.disabled_electives,
                            debug_mode = EXCLUDED.debug_mode,
                            theme = EXCLUDED.theme
                    """, (
                        settings.chat_id, settings.group_name, settings.group_id,
                        settings.change_notifications, settings.daily_notifications,
                        settings.weekly_notifications,
                        psycopg2.extras.Json(settings.pinned_messages),
                        psycopg2.extras.Json(settings.disabled_electives),
                        settings.debug_mode, settings.theme
                    ))
                conn.commit()
        except Exception as e:
            logger.error(f"User save error ({settings.chat_id}): {e}")

    def get_user_settings(self, chat_id: int) -> UserSettings:
        if chat_id not in self.users:
            settings = UserSettings(chat_id)
            self.users[chat_id] = settings
            self._upsert(settings)
        return self.users[chat_id]

    def update_user_group(self, chat_id: int, name: str, group_id: str):
        settings = self.get_user_settings(chat_id)
        settings.group_name = name
        settings.group_id = group_id
        self._upsert(settings)

    def update_user_setting(self, chat_id: int, setting: str, value) -> None:
        settings = self.get_user_settings(chat_id)
        setattr(settings, setting, value)
        self._upsert(settings)

def build_schedule_cache_class(ScheduleEvent, ChangeType, ScheduleChange, TIMEZONE):
    class ScheduleCache:
        def __init__(self):
            self._group_caches: Dict[str, List[ScheduleEvent]] = {}
            self._ensure_schema()
            self._load_all()

        def _ensure_schema(self):
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS schedule_cache (
                            group_id TEXT PRIMARY KEY,
                            events JSONB NOT NULL DEFAULT '[]'::jsonb,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                    """)
                conn.commit()

        def _load_all(self):
            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT group_id, events FROM schedule_cache")
                        for group_id, events_data in cur.fetchall():
                            self._group_caches[group_id] = [
                                ScheduleEvent.from_dict(e) for e in events_data
                            ]
            except Exception as e:
                logger.error(f"Cache load error: {e}")

        def _save_group(self, group_id: str, events: List[ScheduleEvent]):
            try:
                data = [e.to_dict() for e in events]
                with get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO schedule_cache (group_id, events, updated_at)
                            VALUES (%s, %s, now())
                            ON CONFLICT (group_id) DO UPDATE SET
                                events = EXCLUDED.events,
                                updated_at = now()
                        """, (group_id, psycopg2.extras.Json(data)))
                    conn.commit()
            except Exception as e:
                logger.error(f"Cache save error ({group_id}): {e}")

        def update_and_detect_changes(self, group_id: str, new_events: List[ScheduleEvent]) -> List:
            old_events = self._group_caches.get(group_id, [])
            if not old_events and new_events:
                self._group_caches[group_id] = new_events
                self._save_group(group_id, new_events)
                return []

            changes = []
            old_map = {e.get_unique_key(): e for e in old_events}
            new_map = {e.get_unique_key(): e for e in new_events}
            now = datetime.now(TIMEZONE)

            for key, ev in old_map.items():
                if key not in new_map:
                    if ev.end_time < now:
                        continue
                    changes.append(ScheduleChange(ChangeType.REMOVED, ev))
                elif ev.hash != new_map[key].hash:
                    changes.append(ScheduleChange(ChangeType.MODIFIED, new_map[key], ev))

            for key, ev in new_map.items():
                if key not in old_map:
                    if ev.end_time < now:
                        continue
                    changes.append(ScheduleChange(ChangeType.ADDED, ev))

            if changes or (len(old_events) != len(new_events)):
                self._group_caches[group_id] = new_events
                self._save_group(group_id, new_events)
            return changes

    return ScheduleCache

import logging
import os
import re
import json
import hashlib
import asyncio
from datetime import datetime, timedelta, date, time
from typing import List, Dict, Optional
from enum import Enum
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ChatMember
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv
import pytz

# Імпорт генератора картинок
try:
    from image_gen import ScheduleImageGenerator
except ImportError:
    print("⚠️ УВАГА: Файл image_gen.py не знайдено. Генерація картинок не працюватиме.")
    ScheduleImageGenerator = None

load_dotenv()

# --- Configuration ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
TIMEZONE = pytz.timezone('Europe/Kyiv')
DAILY_NOTIFICATION_TIME = time(5, 0)
WEEKLY_NOTIFICATION_TIME = time(15, 00)
WEEKLY_NOTIFICATION_DAY = 0
SCHEDULE_CHECK_INTERVAL = 30 * 60
MAX_PINNED_MESSAGES = 5

# --- Enums & Classes ---

class ChangeType(Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"

class ScheduleEvent:
    def __init__(self, data: dict):
        self.raw_subject = data.get('subject', 'Невідомий предмет')
        self.teacher = data.get('teacher', '')
        self.room = data.get('room', '')
        self.event_type = data.get('type', '')
        self.group = data.get('group', '')
        self.is_remote = data.get('is_remote', False)
        self.links = data.get('links', [])
        self.start_time = data.get('start_time', datetime.now(TIMEZONE))
        self.end_time = data.get('end_time', datetime.now(TIMEZONE))
        
        self.subject = self._clean_subject(self.raw_subject)
        self.hash = self._calculate_hash()

    def _clean_subject(self, text: str) -> str:
        text = re.sub(r'(?i)дистанційно', '', text)
        if self.event_type:
            escaped_type = re.escape(self.event_type)
            text = re.sub(fr'\({escaped_type}\)', '', text, flags=re.IGNORECASE)
        if self.teacher:
            text = text.replace(self.teacher, '')
        
        # Видаляємо підгрупу з назви предмету (бо вона вже є в self.group завдяки парсеру)
        text = re.sub(r'\(підгр\.\s*\d+\)', '', text)

        text = re.sub(r'(доцент|професор|викладач|асистент|зав\.каф\.)\s+[A-ZА-ЯІЇЄ][a-zа-яіїє\']+\s+[A-ZА-ЯІЇЄ][a-zа-яіїє\']+(\s+[A-ZА-ЯІЇЄ][a-zа-яіїє\']+)?', '', text)
        text = re.sub(r'(доцент|професор|викладач|асистент|зав\.каф\.)\s+[A-ZА-ЯІЇЄ][a-zа-яіїє\']+\s+[A-ZА-ЯІЇЄ]\.([A-ZА-ЯІЇЄ]\.)?', '', text)
        text = re.sub(r'\d+[^\s]*\.ауд\.', '', text)
        text = text.replace('*', '').strip()
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _calculate_hash(self) -> str:
        links_str = ",".join(self.links)
        key_data = f"{self.start_time.isoformat()}-{self.subject}-{self.teacher}-{self.room}-{self.group}-{self.is_remote}-{links_str}"
        return hashlib.md5(key_data.encode()).hexdigest()[:8]

    def get_unique_key(self) -> str:
        return f"{self.start_time.strftime('%Y%m%d%H%M')}-{self.subject}-{self.group}"
    
    def to_dict(self) -> dict:
        return {
            'subject': self.raw_subject,
            'teacher': self.teacher,
            'room': self.room,
            'type': self.event_type,
            'group': self.group,
            'is_remote': self.is_remote,
            'links': self.links,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat()
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'ScheduleEvent':
        data['start_time'] = datetime.fromisoformat(data['start_time'])
        data['end_time'] = datetime.fromisoformat(data['end_time'])
        return cls(data)

    def matches_query(self, query: str) -> bool:
        q = query.lower()
        return (q in self.subject.lower() or
                q in self.teacher.lower() or
                q in self.room.lower() or
                q in self.event_type.lower() or
                q in self.group.lower())

class ScheduleChange:
    def __init__(self, change_type: ChangeType, event: ScheduleEvent, old_event: Optional[ScheduleEvent] = None):
        self.change_type = change_type
        self.event = event
        self.old_event = old_event

# --- User Settings & Cache ---

class UserSettings:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.group_name: Optional[str] = None
        self.group_id: Optional[str] = None
        self.change_notifications = False
        self.daily_notifications = False
        self.weekly_notifications = False
        self.pinned_messages: List[int] = []

    def to_dict(self) -> dict:
        return {
            'chat_id': self.chat_id,
            'group_name': self.group_name,
            'group_id': self.group_id,
            'change_notifications': self.change_notifications,
            'daily_notifications': self.daily_notifications,
            'weekly_notifications': self.weekly_notifications,
            'pinned_messages': self.pinned_messages
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
        return settings

class ScheduleCache:
    def __init__(self, cache_file: str = "schedule_cache_global.json"):
        self.cache_file = cache_file
        self._group_caches: Dict[str, List[ScheduleEvent]] = {}
        self._load_cache()

    def _load_cache(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for group_id, events_data in data.items():
                        self._group_caches[group_id] = [ScheduleEvent.from_dict(e) for e in events_data]
        except Exception as e:
            logger.error(f"Cache load error: {e}")

    def _save_cache(self):
        try:
            data = {gid: [e.to_dict() for e in events] for gid, events in self._group_caches.items()}
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Cache save error: {e}")

    def update_and_detect_changes(self, group_id: str, new_events: List[ScheduleEvent]) -> List[ScheduleChange]:
        old_events = self._group_caches.get(group_id, [])
        if not old_events and new_events:
            self._group_caches[group_id] = new_events
            self._save_cache()
            return []

        changes = []
        old_map = {e.get_unique_key(): e for e in old_events}
        new_map = {e.get_unique_key(): e for e in new_events}
        now = datetime.now(TIMEZONE)

        for key, ev in old_map.items():
            if key not in new_map:
                if ev.end_time < now: continue
                changes.append(ScheduleChange(ChangeType.REMOVED, ev))
            elif ev.hash != new_map[key].hash:
                changes.append(ScheduleChange(ChangeType.MODIFIED, new_map[key], ev))

        for key, ev in new_map.items():
            if key not in old_map:
                if ev.end_time < now: continue
                changes.append(ScheduleChange(ChangeType.ADDED, ev))

        if changes or (len(old_events) != len(new_events)):
            self._group_caches[group_id] = new_events
            self._save_cache()
        return changes

class UserManager:
    def __init__(self, settings_file: str = "user_settings.json"):
        self.settings_file = settings_file
        self.users: Dict[int, UserSettings] = {}
        self._load_settings()

    def _load_settings(self):
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for user_data in data.get('users', []):
                        settings = UserSettings.from_dict(user_data)
                        self.users[settings.chat_id] = settings
        except Exception: self.users = {}

    def _save_settings(self):
        try:
            data = {'users': [s.to_dict() for s in self.users.values()]}
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e: logger.error(f"Settings save error: {e}")

    def get_user_settings(self, chat_id: int) -> UserSettings:
        if chat_id not in self.users:
            self.users[chat_id] = UserSettings(chat_id)
            self._save_settings()
        return self.users[chat_id]

    def update_user_group(self, chat_id: int, name: str, group_id: str):
        settings = self.get_user_settings(chat_id)
        settings.group_name = name
        settings.group_id = group_id
        self._save_settings()

    def update_user_setting(self, chat_id: int, setting: str, value: any):
        settings = self.get_user_settings(chat_id)
        setattr(settings, setting, value)
        self._save_settings()

# --- Parsing Logic ---

class NungParser:
    API_URL = "https://dekanat.nung.edu.ua/cgi-bin/timetable_export.cgi"
    HTML_URL = "https://dekanat.nung.edu.ua/cgi-bin/timetable.cgi?n=700"
    _global_cache = {'teachers': [], 'rooms': [], 'timestamp': None}

    @staticmethod
    def _normalize(text):
        if not text: return ""
        text = text.lower().replace("–", "-").replace("—", "-").replace(" ", "").replace("`", "").replace("'", "").replace("'", "")
        trans_table = str.maketrans({'i': 'і', 'k': 'к', 'c': 'с', 'o': 'о', 'p': 'р', 'x': 'х', 'a': 'а', 'e': 'е', 'h': 'н', 't': 'т', 'm': 'м', 'b': 'в'})
        return text.translate(trans_table)

    @staticmethod
    def get_group_id(group_name: str) -> Optional[str]:
        params = {'req_type': 'obj_list', 'req_mode': 'group', 'show_ID': 'yes', 'req_format': 'json', 'coding_mode': 'WINDOWS-1251', 'bs': 'ok'}
        try:
            response = requests.get(NungParser.API_URL, params=params, timeout=10)
            try: data = response.json()
            except: 
                response = requests.get(NungParser.API_URL, params={**params, 'coding_mode': 'UTF-8'}, timeout=10)
                data = response.json()
            target = NungParser._normalize(group_name)
            root = data.get('psrozklad_export') or data.get('ps_rozklad_export')
            if root:
                for dept in root.get('departments', []):
                    for obj in dept.get('objects', []):
                        if NungParser._normalize(obj.get('name', '')) == target: return obj.get('ID')
            return None
        except Exception as e:
            logger.error(f"Group Search Error: {e}")
            return None

    @staticmethod
    def search_global(query: str) -> List[Dict]:
        now = datetime.now()
        if not NungParser._global_cache['timestamp'] or (now - NungParser._global_cache['timestamp']).seconds > 3600:
            NungParser._global_cache['teachers'] = NungParser._fetch_objects('teacher')
            NungParser._global_cache['rooms'] = NungParser._fetch_objects('room')
            NungParser._global_cache['timestamp'] = now
        query_norm = NungParser._normalize(query)
        results = []
        for t in NungParser._global_cache['teachers']:
            if query_norm in NungParser._normalize(t.get('name', '')):
                results.append({'type_label': 'Викладач', 'type_code': 't', 'name': t['name'], 'id': t['ID']})
        for r in NungParser._global_cache['rooms']:
            if query_norm in NungParser._normalize(r.get('name', '')):
                results.append({'type_label': 'Аудиторія', 'type_code': 'r', 'name': r['name'], 'id': r['ID']})
        return results[:20]

    @staticmethod
    def _fetch_objects(req_mode: str) -> List[Dict]:
        params = {'req_type': 'obj_list', 'req_mode': req_mode, 'show_ID': 'yes', 'req_format': 'json', 'coding_mode': 'WINDOWS-1251', 'bs': 'ok'}
        try:
            response = requests.get(NungParser.API_URL, params=params, timeout=15)
            response.encoding = response.apparent_encoding
            data = response.json()
            objects = []
            root = data.get('psrozklad_export') or data.get('ps_rozklad_export')
            if root:
                if req_mode == 'teacher':
                    for dept in root.get('departments', []): objects.extend(dept.get('objects', []))
                elif req_mode == 'room':
                    for block in root.get('blocks', []): objects.extend(block.get('objects', []))
            return objects
        except: return []
    
    @staticmethod
    def _fetch_links_map(group_name: str) -> Dict[str, str]:
        links_map = {}
        if not group_name: return links_map
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://dekanat.nung.edu.ua/cgi-bin/timetable.cgi?n=700'
        }
        
        try:
            payload = {
                'n': '700', 'faculty': '0', 'teacher': '', 'course': '0',
                'group': group_name.encode('windows-1251'), 
                'sdate': '', 'edate': ''
            }
            
            response = requests.post(NungParser.HTML_URL, data=payload, headers=headers, timeout=8)
            response.encoding = 'windows-1251'
            soup = BeautifulSoup(response.text, 'html.parser')
            
            for link_div in soup.find_all('div', class_='link'):
                a_tag = link_div.find('a', href=True)
                if not a_tag: continue
                
                url = a_tag['href']
                if not any(x in url for x in ['google.com', 'zoom.us', 'teams', 'webex']):
                    continue

                curr = link_div.previous_sibling
                found_name = None
                
                for _ in range(5):
                    if not curr: break
                    text = ""
                    if isinstance(curr, str): text = curr.strip()
                    elif hasattr(curr, 'get_text'): text = curr.get_text(strip=True)
                    
                    if len(text) > 3:
                        match = re.search(r'([A-ZА-ЯІЇЄ][a-zа-яіїє\']+)\s+[A-ZА-ЯІЇЄ]', text)
                        if match:
                            found_name = match.group(1)
                            break
                    curr = curr.previous_sibling

                if found_name:
                    links_map[found_name] = url
                        
        except Exception as e:
            logger.error(f"HTML Link Parsing Error: {e}")
            
        return links_map

    @staticmethod
    def get_schedule(obj_id: str, start_date: date = None, end_date: date = None, obj_type: str = 'group', group_name: str = None) -> List[ScheduleEvent]:
        if not start_date: start_date = datetime.now(TIMEZONE).date() - timedelta(days=1)
        if not end_date: end_date = datetime.now(TIMEZONE).date() + timedelta(days=30)
        
        links_map = {}
        if obj_type == 'group' and group_name:
            links_map = NungParser._fetch_links_map(group_name)

        return NungParser.get_schedule_json(obj_id, obj_type, start_date, end_date, links_map)

    @staticmethod
    def _split_merged_events(description: str) -> List[str]:
        subgroups = list(re.finditer(r'\(підгр\.\s*\d+\)', description))
        if len(subgroups) < 2: return [description]
        results = []
        prev_split = 0
        for i in range(len(subgroups)):
            match = subgroups[i]
            if i < len(subgroups) - 1:
                next_match = subgroups[i+1]
                search_start = match.end()
                search_end = next_match.start()
                segment = description[search_start:search_end]
                room_match = re.search(r'\d+[^\s]*\.ауд\.', segment)
                if room_match: split_point = search_start + room_match.end()
                else:
                    teacher_match = re.search(r'[A-ZА-ЯІЇЄ]\.[A-ZА-ЯІЇЄ]\.', segment)
                    if teacher_match: split_point = search_start + teacher_match.end()
                    else:
                        split_point = next_match.start()
                        last_caps = list(re.finditer(r'[A-ZА-ЯІЇЄ][a-zа-яіїє]+', segment))
                        if last_caps: split_point = search_start + last_caps[-1].start()
            else: split_point = len(description)
            chunk = description[prev_split:split_point].strip()
            if chunk: results.append(chunk)
            prev_split = split_point
        return results

    @staticmethod
    def get_schedule_json(obj_id: str, obj_mode: str, start_date: date, end_date: date, links_map: Dict[str, str] = None) -> List[ScheduleEvent]:
        params = {
            'req_type': 'rozklad', 'req_mode': obj_mode, 'OBJ_ID': obj_id,
            'ros_text': 'separated', 'begin_date': start_date.strftime('%d.%m.%Y'),
            'end_date': end_date.strftime('%d.%m.%Y'), 'req_format': 'json', 'coding_mode': 'UTF8', 'bs': 'ok'
        }
        try:
            response = requests.get(NungParser.API_URL, params=params, timeout=15)
            response.encoding = 'utf-8'
            data = response.json()
            events = []
            root = data.get('psrozklad_export') or data.get('ps_rozklad_export')
            items = root.get('roz_items', []) if root else []

            for item in items:
                original_desc = item.get('lesson_description', '').strip() or \
                                f"{item.get('title', '')} {item.get('teacher', '')} {item.get('room', '')}"
                
                descriptions = NungParser._split_merged_events(original_desc)
                json_link = item.get('link') or item.get('url') or ""
                
                for description in descriptions:
                    date_str = item.get('date')
                    time_range = item.get('lesson_time', '').split('-')
                    if len(time_range) != 2: continue
                    try:
                        date_obj = datetime.strptime(date_str, '%d.%m.%Y').date()
                        start_time = datetime.strptime(time_range[0].strip(), '%H:%M').time()
                        end_time = datetime.strptime(time_range[1].strip(), '%H:%M').time()
                        start_dt = TIMEZONE.localize(datetime.combine(date_obj, start_time))
                        end_dt = TIMEZONE.localize(datetime.combine(date_obj, end_time))
                    except ValueError: continue

                    room = item.get('room') or ""
                    if not room:
                        room_match = re.search(r'(\d+[^\s]*\.ауд\.)', description)
                        if room_match: room = room_match.group(1)

                    event_type = item.get('type') or ""
                    if not event_type:
                        type_match = re.search(r'\((Л|Пр|Лаб|Л\+Пр|Sem|Екз|Конс)\)', description)
                        if type_match: event_type = type_match.group(1)

                    clean_text = description.replace('*', '').strip()
                    teacher_name = item.get('teacher') or ""
                    
                    # --- ОСЬ ЦЕ ПОВЕРНУТА ЛОГІКА З РОБОЧОЇ ВЕРСІЇ ---
                    # Ми тут визначаємо групу разом з підгрупою, додаючи пробіл
                    group_name = item.get('object') or ""
                    subgroup_match = re.search(r'\(підгр\.\s*(\d+)\)', clean_text)
                    if subgroup_match: 
                        # Це той самий рядок, який "форсує" пробіл, як у вашій старій версії
                        group_name = f"(підгр. {subgroup_match.group(1)})"
                    # ------------------------------------------------

                    if not teacher_name and obj_mode == 'group':
                        tm = re.search(r'(доцент|професор|викладач|асистент|зав\.каф\.)\s+[A-ZА-ЯІЇЄ][a-zа-яіїє\']+\s+[A-ZА-ЯІЇЄ][a-zа-яіїє\']+(\s+[A-ZА-ЯІЇЄ][a-zа-яіїє\']+)?', clean_text)
                        if tm: teacher_name = tm.group(0)

                    is_remote = (item.get('online') in ['Tak', 'Yes', '1', 'Так', 'True']) or "дистанційно" in clean_text.lower()
                    clean_text = re.sub(r'(?i)дистанційно', '', clean_text).strip()
                    if not clean_text and item.get('title'): clean_text = item.get('title')

                    final_links = []
                    if json_link:
                        final_links.append(json_link)
                    
                    if not final_links and links_map and teacher_name:
                        for surname, html_link in links_map.items():
                            if surname in teacher_name:
                                final_links.append(html_link)
                                break

                    event_data = {
                        'subject': clean_text, 'type': event_type, 'teacher': teacher_name, 
                        'room': room, 'group': group_name, 'is_remote': is_remote, 'links': final_links, 
                        'start_time': start_dt, 'end_time': end_dt
                    }
                    events.append(ScheduleEvent(event_data))
            
            events.sort(key=lambda x: x.start_time)
            return events
        except Exception as e:
            logger.error(f"JSON Parse Error: {e}")
            return []

class ScheduleFormatter:
    @classmethod
    def _build_event_details(cls, event: ScheduleEvent, strikethrough: bool = False) -> str:
        lines = []
        subj_line = f"📚 {event.subject}"
        if event.group: subj_line += f" {event.group}"
        if event.event_type: subj_line += f" ({event.event_type})"
        if strikethrough: lines.append(f"<s>{subj_line}</s>")
        else:
            if event.is_remote: lines.append("💻🏡 <b>ДИСТАНЦІЙНО</b>")
            lines.append(subj_line)
            if event.teacher: lines.append(f"👤🎓 {event.teacher}")
            if event.room: lines.append(f"📍 {event.room}")
            
            if event.links:
                for link in event.links:
                    link_name = "Посилання на пару 🔗"
                    if "zoom" in link: link_name = "Zoom 🎥"
                    elif "meet.google" in link: link_name = "Google Meet 🎥"
                    elif "teams" in link: link_name = "Teams 🎥"
                    lines.append(f'<a href="{link}">{link_name}</a>')
        
        return "\n".join(lines)

    @classmethod
    def format_changes(cls, changes: List[ScheduleChange]) -> str:
        if not changes: return ""
        res = "🔄 <b>Зміни у розкладі:</b>\n\n"
        for c in changes:
            d_str = c.event.start_time.strftime('%d.%m')
            time_s = c.event.start_time.strftime('%H:%M')
            details = cls._build_event_details(c.event, strikethrough=(c.change_type == ChangeType.REMOVED))
            if c.change_type == ChangeType.ADDED:
                res += f"✅ <b>Додано ({d_str} | {time_s}):</b>\n{details}\n\n"
            elif c.change_type == ChangeType.REMOVED:
                res += f"❌ <b>Скасовано ({d_str} | {time_s}):</b>\n{details}\n\n"
            elif c.change_type == ChangeType.MODIFIED:
                res += f"✏️ <b>Змінено ({d_str} | {time_s}):</b>\n{details}\n\n"
        return res
        
    @classmethod
    def split_long_message(cls, text: str, max_length: int = 4000) -> List[str]:
        if len(text) <= max_length: return [text]
        parts = []
        while text:
            if len(text) <= max_length: parts.append(text); break
            split_pos = text.rfind('\n', 0, max_length)
            if split_pos == -1: split_pos = max_length
            parts.append(text[:split_pos])
            text = text[split_pos:].lstrip()
        return parts

class ScheduleBot:
    def __init__(self):
        self.formatter = ScheduleFormatter()
        self.user_manager = UserManager()
        self.cache_manager = ScheduleCache()
        self.image_generator = ScheduleImageGenerator(font_path="/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Regular.ttf") if ScheduleImageGenerator else None
        self.application = None
        self._schedule_check_running = False

    def set_application(self, application):
        self.application = application
        self.application.job_queue.run_daily(self._daily_notification_job, time=DAILY_NOTIFICATION_TIME)
        self.application.job_queue.run_daily(self._weekly_notification_job, time=WEEKLY_NOTIFICATION_TIME, days=[WEEKLY_NOTIFICATION_DAY])
        self.application.job_queue.run_repeating(self._check_schedule_changes_job, interval=SCHEDULE_CHECK_INTERVAL, first=30)

    async def _is_user_admin(self, update: Update) -> bool:
        if update.effective_chat.type == ChatType.PRIVATE: return True
        try:
            member = await update.effective_chat.get_member(update.effective_user.id)
            return member.status in [ChatMember.OWNER, ChatMember.ADMINISTRATOR]
        except: return False

    def _get_events(self, group_id: str, group_name: str = None) -> List[ScheduleEvent]:
        return NungParser.get_schedule(group_id, obj_type='group', group_name=group_name)

    async def _pin_message_with_management(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
        settings = self.user_manager.get_user_settings(chat_id)
        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
            settings.pinned_messages.append(message_id)
            if len(settings.pinned_messages) > MAX_PINNED_MESSAGES:
                oldest_message_id = settings.pinned_messages.pop(0)
                try: await context.bot.unpin_chat_message(chat_id=chat_id, message_id=oldest_message_id)
                except Exception as e: logger.warning(f"Unpin error: {e}")
            self.user_manager.update_user_setting(chat_id, 'pinned_messages', settings.pinned_messages)
        except Exception as e: logger.error(f"Pin error: {e}")

    # --- Jobs ---
    async def _weekly_notification_job(self, context: ContextTypes.DEFAULT_TYPE):
        users = [uid for uid, s in self.user_manager.users.items() if s.weekly_notifications]
        tomorrow = datetime.now(TIMEZONE).date() + timedelta(days=1)
        for chat_id in users:
            s = self.user_manager.get_user_settings(chat_id)
            if not s.group_id: continue
            events = NungParser.get_schedule(s.group_id, start_date=tomorrow, end_date=tomorrow + timedelta(days=6), group_name=s.group_name)
            if not events: continue
            if self.image_generator:
                photo_bio = self.image_generator.create_week_image(events, tomorrow)
                try:
                    msg = await context.bot.send_photo(chat_id=chat_id, photo=photo_bio, caption=f"📅 Тиждень: {s.group_name}")
                    await self._pin_message_with_management(context, chat_id, msg.message_id)
                except Exception as e: logger.error(f"Weekly error: {e}")

    async def _daily_notification_job(self, context: ContextTypes.DEFAULT_TYPE):
        today = datetime.now(TIMEZONE).date()
        users = [uid for uid, s in self.user_manager.users.items() if s.daily_notifications]
        for chat_id in users:
            s = self.user_manager.get_user_settings(chat_id)
            if not s.group_id: continue
            all_events = NungParser.get_schedule(s.group_id, group_name=s.group_name)
            events = [e for e in all_events if e.start_time.date() == today]
            if not events: continue
            if self.image_generator:
                photo_bio = self.image_generator.create_day_image(events, today)
                try: 
                    msg = await context.bot.send_photo(chat_id=chat_id, photo=photo_bio, caption=f"📅 Сьогодні: {s.group_name}")
                    await self._pin_message_with_management(context, chat_id, msg.message_id)
                except Exception as e: logger.error(f"Daily error: {e}")

    async def _check_schedule_changes_job(self, context: ContextTypes.DEFAULT_TYPE):
        if self._schedule_check_running: return
        self._schedule_check_running = True
        try:
            active_groups = {} 
            for s in self.user_manager.users.values():
                if s.group_id and s.change_notifications: 
                    active_groups[s.group_id] = s.group_name
            
            for group_id, group_name in active_groups.items():
                new_events = NungParser.get_schedule(group_id, obj_type='group', group_name=group_name)
                if not new_events:
                    old_events = self.cache_manager._group_caches.get(group_id, [])
                    if old_events: continue
                changes = self.cache_manager.update_and_detect_changes(group_id, new_events)
                if changes:
                    text_changes = self.formatter.format_changes(changes)
                    if not text_changes.strip(): continue
                    targets = [uid for uid, s in self.user_manager.users.items() if s.group_id == group_id and s.change_notifications]
                    for chat_id in targets:
                        try: 
                            msg = await context.bot.send_message(chat_id=chat_id, text=text_changes, parse_mode=ParseMode.HTML, disable_notification=True)
                            await self._pin_message_with_management(context, chat_id, msg.message_id)
                        except: pass
        except Exception as e: logger.error(f"Check job error: {e}")
        finally: self._schedule_check_running = False

    # --- Commands ---
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = self.user_manager.get_user_settings(update.effective_chat.id)
        msg = f"👋 Привіт!\n"
        if s.group_name: msg += f"✅ Група: <b>{s.group_name}</b>\n"
        else: msg += "⚠️ Напишіть: <code>/group Назва</code>\n"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=self.get_main_keyboard(), disable_notification=True)

    async def group_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_user_admin(update): return await update.message.reply_text("⛔ Тільки адміністратори чату можуть змінювати групу.")
        if not context.args: return await update.message.reply_text("❌ Приклад: `/group КІ-24-1`", parse_mode=ParseMode.MARKDOWN)
        group_name = " ".join(context.args)
        group_id = NungParser.get_group_id(group_name)
        if group_id:
            self.user_manager.update_user_group(update.effective_chat.id, group_name.upper(), group_id)
            events = NungParser.get_schedule(group_id, obj_type='group', group_name=group_name.upper())
            self.cache_manager.update_and_detect_changes(group_id, events)
            await update.message.reply_text(f"✅ Збережено: <b>{group_name.upper()}</b>", parse_mode=ParseMode.HTML, reply_markup=self.get_main_keyboard())
        else: await update.message.reply_text("❌ Групу не знайдено.")

    async def _send_schedule_image(self, update: Update, events: List[ScheduleEvent], date_obj: date, mode: str, caption: str):
        if not self.image_generator: 
            text_response = caption + "\n\n"
            for e in events:
                text_response += self.formatter._build_event_details(e) + "\n\n"
            if update.callback_query: await update.callback_query.message.edit_text(text_response, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            else: await update.effective_chat.send_message(text_response, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return

        if mode == 'week': bio = self.image_generator.create_week_image(events, date_obj)
        else: bio = self.image_generator.create_day_image(events, date_obj)

        subject_links = {} 
        for e in events:
            if not e.links: continue
            for link in e.links:
                if e.subject not in subject_links: subject_links[e.subject] = {}
                if link not in subject_links[e.subject]: subject_links[e.subject][link] = []
                subject_links[e.subject][link].append(e.start_time)

        links_text_lines = []
        for subject, links_data in subject_links.items():
            if len(links_data) == 1:
                link = list(links_data.keys())[0]
                link_name = "Zoom/Meet"
                if "zoom" in link: link_name = "Zoom 🎥"
                elif "meet" in link: link_name = "Meet 🎥"
                links_text_lines.append(f"📚 {subject}: <a href=\"{link}\">{link_name}</a>")
            else:
                for link, times in links_data.items():
                    link_name = "Zoom/Meet"
                    if "zoom" in link: link_name = "Zoom 🎥"
                    elif "meet" in link: link_name = "Meet 🎥"
                    time_strs = [t.strftime("%d.%m %H:%M") for t in times]
                    time_str = ", ".join(time_strs)
                    links_text_lines.append(f"📚 {subject} ({time_str}): <a href=\"{link}\">{link_name}</a>")

        full_caption = caption
        if links_text_lines: 
            full_caption += "\n\n🔗 <b>Посилання:</b>\n" + "\n".join(links_text_lines)
        
        prev_date = (date_obj - timedelta(days=1)).strftime("%Y-%m-%d")
        next_date = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
        if mode == 'week':
            prev_date = (date_obj - timedelta(days=7)).strftime("%Y-%m-%d")
            next_date = (date_obj + timedelta(days=7)).strftime("%Y-%m-%d")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️", callback_data=f"sched|{mode}|{prev_date}"),
             InlineKeyboardButton("Сьогодні", callback_data=f"sched|{mode}|today"),
             InlineKeyboardButton("➡️", callback_data=f"sched|{mode}|{next_date}")],
            [InlineKeyboardButton("◀️ Меню", callback_data="back")]
        ])

        if update.callback_query:
            if update.callback_query.message.photo:
                media = InputMediaPhoto(media=bio, caption=full_caption, parse_mode=ParseMode.HTML)
                try: await update.callback_query.edit_message_media(media=media, reply_markup=kb)
                except Exception as e: logger.warning(f"Edit media warning: {e}")
            else:
                await update.callback_query.message.delete()
                await update.effective_chat.send_photo(photo=bio, caption=full_caption, reply_markup=kb, parse_mode=ParseMode.HTML, disable_notification=True)
        else:
            await update.effective_chat.send_photo(photo=bio, caption=full_caption, reply_markup=kb, parse_mode=ParseMode.HTML, disable_notification=True)

    async def _generic_schedule_command(self, update: Update, mode='today', target_date=None):
        s = self.user_manager.get_user_settings(update.effective_chat.id)
        if not s.group_id: return await update.effective_message.reply_text("⚠️ Оберіть групу: `/group Назва`")

        now = datetime.now(TIMEZONE).date()
        if not target_date:
            target_date = now if mode == 'today' else (now + timedelta(days=1))
            if mode == 'week': target_date = now 
        
        if mode == 'week': target_date = target_date - timedelta(days=target_date.weekday())

        events = self._get_events(s.group_id, s.group_name)
        if mode == 'week':
            filtered_events = [e for e in events if target_date <= e.start_time.date() <= target_date + timedelta(days=6)]
            caption = f"📅 Розклад: {s.group_name}"
        else:
            filtered_events = [e for e in events if e.start_time.date() == target_date]
            caption = f"📅 {target_date.strftime('%d.%m')} - {s.group_name}"

        await self._send_schedule_image(update, filtered_events, target_date, mode, caption)

    async def today_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._generic_schedule_command(update, 'today')
    async def tomorrow_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._generic_schedule_command(update, 'tomorrow')
    async def week_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._generic_schedule_command(update, 'week')

    async def date_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args: return await update.message.reply_text("📅 Формат: `/date 19.12`", parse_mode=ParseMode.MARKDOWN)
        try:
            day, month = map(int, context.args[0].split('.'))
            target_date = date(datetime.now().year, month, day)
            await self._generic_schedule_command(update, 'date', target_date)
        except: await update.message.reply_text("❌ Невірний формат.")

    async def search_all_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args: return await update.message.reply_text("🔍 Приклад: `/search_all Коваль`")
        results = NungParser.search_global(" ".join(context.args))
        if not results: return await update.message.reply_text("❌ Нічого не знайдено.")

        keyboard = []
        for res in results:
            callback_data = f"view_sched_img|{res['type_code']}|{res['id']}|today"
            type_icon = "👨‍🏫" if res['type_code'] == 't' else "🚪"
            btn_text = f"{type_icon} {res['name']}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
        
        keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data="delete_msg")])
        await update.message.reply_text(f"🔍 Знайдено {len(results)}:", reply_markup=InlineKeyboardMarkup(keyboard), disable_notification=True)
        
    async def search_local_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = self.user_manager.get_user_settings(update.effective_chat.id)
        if not s.group_id: return await update.message.reply_text("⚠️ Оберіть групу.")
        if not context.args: return await update.message.reply_text("🔍 Приклад: `/search Математика`", parse_mode=ParseMode.MARKDOWN)
        
        query = " ".join(context.args)
        events = self._get_events(s.group_id, s.group_name)
        found = [e for e in events if e.matches_query(query) and e.start_time.date() >= datetime.now(TIMEZONE).date()]
        if not found: return await update.message.reply_text("📭 Нічого не знайдено.")
        
        text = f"🔍 Результати для '{query}':\n\n"
        for e in found[:10]:
            text += self.formatter._build_event_details(e) + f"\n📆 {e.start_time.strftime('%d.%m')} {e.start_time.strftime('%H:%M')}\n\n"
        for part in self.formatter.split_long_message(text):
            await update.message.reply_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True, disable_notification=True)

    async def notifications_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        s = self.user_manager.get_user_settings(chat_id)
        is_admin = await self._is_user_admin(update)
        
        kb_rows = []
        if is_admin:
            kb_rows.append([InlineKeyboardButton(f"Сповіщення про зміни {'✅' if s.change_notifications else '❌'}", callback_data="toggle_changes")])
            kb_rows.append([InlineKeyboardButton(f"Щоденно о {DAILY_NOTIFICATION_TIME} {'✅' if s.daily_notifications else '❌'}", callback_data="toggle_daily")])
            kb_rows.append([InlineKeyboardButton(f"Розклад на тиждень о {WEEKLY_NOTIFICATION_TIME} {'✅' if s.weekly_notifications else '❌'}", callback_data="toggle_weekly")])
        
        kb_rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
        text = f"⚙️ Група: <b>{s.group_name}</b>"
        if not is_admin and update.effective_chat.type != ChatType.PRIVATE:
            text += "\n🔒 <i>Налаштування доступні лише адміністраторам.</i>"

        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.HTML)
            except Exception:
                await update.callback_query.message.delete()
                await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.HTML, disable_notification=True)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.HTML)

    def get_main_keyboard(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Сьогодні", callback_data="today"), InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")],
            [InlineKeyboardButton("📊 Тиждень", callback_data="week"), InlineKeyboardButton("⚙️ Меню", callback_data="notifications")]
        ])

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data

        if data == "delete_msg": await query.message.delete()
        elif data == "back":
            if query.message.text:
                try:
                    await query.edit_message_text("🏠 Головне меню:", reply_markup=self.get_main_keyboard())
                except:
                    await query.message.delete()
                    await query.message.chat.send_message("🏠 Головне меню:", reply_markup=self.get_main_keyboard(), disable_notification=True)
            else:
                await query.message.delete()
                await query.message.chat.send_message("🏠 Головне меню:", reply_markup=self.get_main_keyboard(), disable_notification=True)
        
        elif data in ['today', 'tomorrow', 'week']: await self._generic_schedule_command(update, data)
        elif data == "notifications": await self.notifications_command(update, context)
            
        elif data.startswith("toggle_"):
            if not await self._is_user_admin(update):
                await query.answer("⛔ Тільки адміністратори можуть змінювати налаштування!", show_alert=True)
                return
            
            if data == "toggle_changes": setting = "change_notifications"
            elif data == "toggle_daily": setting = "daily_notifications"
            elif data == "toggle_weekly": setting = "weekly_notifications"
            else: return
            
            curr = getattr(self.user_manager.get_user_settings(update.effective_chat.id), setting)
            self.user_manager.update_user_setting(update.effective_chat.id, setting, not curr)
            await self.notifications_command(update, context)
            await query.answer()

        elif data.startswith("sched|"):
            await query.answer()
            parts = data.split("|")
            mode, date_str = parts[1], parts[2]
            target_date = datetime.now(TIMEZONE).date() if date_str == "today" else datetime.strptime(date_str, "%Y-%m-%d").date()
            await self._generic_schedule_command(update, mode, target_date)

        elif data.startswith("view_sched_img|"):
            await query.answer()
            parts = data.split("|")
            type_code, obj_id, date_str = parts[1], parts[2], parts[3]
            target_date = datetime.now(TIMEZONE).date() if date_str == "today" else datetime.strptime(date_str, "%Y-%m-%d").date()
            obj_mode = 'teacher' if type_code == 't' else 'room'
            
            events = NungParser.get_schedule(obj_id, start_date=target_date, end_date=target_date, obj_type=obj_mode)
            
            if self.image_generator:
                bio = self.image_generator.create_day_image(events, target_date)
                prev_date = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
                next_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
                
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️", callback_data=f"view_sched_img|{type_code}|{obj_id}|{prev_date}"),
                     InlineKeyboardButton("Сьогодні", callback_data=f"view_sched_img|{type_code}|{obj_id}|today"),
                     InlineKeyboardButton("➡️", callback_data=f"view_sched_img|{type_code}|{obj_id}|{next_date}")],
                    [InlineKeyboardButton("❌ Закрити", callback_data="delete_msg")]
                ])
                
                if query.message.photo: await query.edit_message_media(media=InputMediaPhoto(bio, caption=f"Розклад: {obj_id}"), reply_markup=kb)
                else:
                    await query.message.delete()
                    await query.message.chat.send_photo(photo=bio, caption=f"Розклад: {obj_id}", reply_markup=kb, disable_notification=True)

def main():
    if not BOT_TOKEN: logger.error("BOT_TOKEN missing"); return
    application = Application.builder().token(BOT_TOKEN).build()
    bot = ScheduleBot()
    bot.set_application(application)

    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("group", bot.group_command))
    application.add_handler(CommandHandler("settings", bot.notifications_command))
    application.add_handler(CommandHandler("search_all", bot.search_all_command))
    application.add_handler(CommandHandler("search", bot.search_local_command))
    application.add_handler(CommandHandler("date", bot.date_command))
    application.add_handler(CommandHandler("today", bot.today_command))
    application.add_handler(CommandHandler("tomorrow", bot.tomorrow_command))
    application.add_handler(CommandHandler("week", bot.week_command))

    application.add_handler(CallbackQueryHandler(bot.button_callback))
    
    logger.info("Bot started...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()

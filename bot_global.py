import logging
import os
import re
import json
import hashlib
import asyncio
import traceback
import html
from datetime import datetime, timedelta, date, time
from typing import List, Dict, Optional
from enum import Enum
from io import BytesIO

import requests
import urllib3
from bs4 import BeautifulSoup, Tag
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ChatMember
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv
import pytz

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from image_gen import ScheduleImageGenerator
except ImportError:
    print("⚠️ УВАГА: Файл image_gen.py не знайдено. Генерація картинок не працюватиме.")
    ScheduleImageGenerator = None

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
TIMEZONE = pytz.timezone('Europe/Kyiv')
DAILY_NOTIFICATION_TIME = time(4, 0)
WEEKLY_NOTIFICATION_TIME = time(14, 00)
WEEKLY_NOTIFICATION_DAY = 0
SCHEDULE_CHECK_INTERVAL = 30 * 60
MAX_PINNED_MESSAGES = 5

PAIR_TIMES = {
    1: ("08:00", "09:20"),
    2: ("09:30", "10:50"),
    3: ("11:00", "12:20"),
    4: ("12:50", "14:10"),
    5: ("14:20", "15:40"),
    6: ("15:50", "17:10"),
    7: ("17:20", "18:40"),
    8: ("18:50", "20:10")
}

PAIR_EMOJIS = {
    1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣",
    5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣"
}

def get_pair_number(start_time) -> int:
    time_str = start_time.strftime("%H:%M")
    for pair_num, (start, end) in PAIR_TIMES.items():
        if time_str == start:
            return pair_num
    return 0

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
        self.is_cancelled = data.get('is_cancelled', False)
        
        self.is_elective = "*(в)" in self.raw_subject.lower() or "військова підготовка" in self.raw_subject.lower()
        self.is_unselected = False 

        self.subject = self._clean_subject(self.raw_subject)
        
        if not self.subject.strip():
            self.subject = self.raw_subject

        if self.is_cancelled:
            self.subject = f"[Увага! ЗАНЯТТЯ ВІДМІНЕНО!]{self.subject}"

        self.hash = self._calculate_hash()

    def _clean_subject(self, text: str) -> str:
        text = re.sub(r'(?i)Увага!\s*Заняття\s*відмінено!?\s*', '', text)
        text = re.sub(r'(?i)дистанційно', '', text)
        if self.event_type:
            escaped_type = re.escape(self.event_type)
            text = re.sub(fr'\({escaped_type}\)', '', text, flags=re.IGNORECASE)
        if self.teacher:
            text = text.replace(self.teacher, '')
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
            'end_time': self.end_time.isoformat(),
            'is_cancelled': self.is_cancelled
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

from storage_postgres import UserSettings, UserManager, build_schedule_cache_class
ScheduleCache = build_schedule_cache_class(ScheduleEvent, ChangeType, ScheduleChange, TIMEZONE)


class NungParser:
    API_URL = "https://dekanat.nung.edu.ua/cgi-bin/timetable_export.cgi"
    HTML_URL = "https://dekanat.nung.edu.ua/cgi-bin/timetable.cgi"
    _global_cache = {'teachers': [], 'rooms': [], 'timestamp': None}

    _URL_RE = re.compile(r'https?://[^\s<>"\']+')
    _TITLES_RE = re.compile(
        r'(?i)(зав\.\s*кафедрою|зав\.\s*каф\.|старший\s+викладач|ст\.\s*викл\.|доцент|професор|викладач|асистент)'
    )

    @staticmethod
    def _normalize(text):
        if not text:
            return ""
        text = text.lower().replace("–", "-").replace("—", "-").replace(" ", "").replace("`", "").replace("'", "").replace("'", "")
        trans_table = str.maketrans({'i': 'і', 'k': 'к', 'c': 'с', 'o': 'о', 'p': 'р',
                                    'x': 'х', 'a': 'а', 'e': 'е', 'h': 'н', 't': 'т', 'm': 'м', 'b': 'в'})
        return text.translate(trans_table)

    @staticmethod
    def _key(text: str) -> str:
        text = NungParser._normalize(text or "").replace('(в)', '')
        return re.sub(r'[^\w]', '', text)

    @staticmethod
    def _teacher_key(text: str) -> str:
        text = NungParser._TITLES_RE.sub('', text or '').replace('*', '').strip()
        tokens = text.split()
        return NungParser._key(tokens[0]) if tokens else ""

    @staticmethod
    def get_group_id(group_name: str) -> tuple[Optional[str], str]:
        params = {'req_type': 'obj_list', 'req_mode': 'group', 'show_ID': 'yes',
                  'req_format': 'json', 'coding_mode': 'WINDOWS-1251', 'bs': 'ok'}
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        try:
            response = requests.get(NungParser.API_URL, params=params, headers=headers, timeout=10, verify=False)
            try:
                data = response.json()
            except:
                response = requests.get(NungParser.API_URL, params={
                                        **params, 'coding_mode': 'UTF-8'}, headers=headers, timeout=10, verify=False)
                data = response.json()

            if isinstance(data, dict) and 'code' in data and int(data['code']) < 0:
                err_msg = data.get('error_message', 'Сервер тимчасово не працює')
                logger.warning(f"Деканат віддав помилку: {err_msg}")
                return None, f"Відповідь деканату: {err_msg}"

            target = NungParser._normalize(group_name)
            root = data.get('psrozklad_export') or data.get('ps_rozklad_export')
            if root:
                for dept in root.get('departments', []):
                    for obj in dept.get('objects', []):
                        if NungParser._normalize(obj.get('name', '')) == target:
                            return obj.get('ID'), "OK"
            return None, "Таку групу не знайдено у списку деканату. Перевірте правильність написання."
        except Exception as e:
            logger.error(f"Group Search Error: {e}")
            return None, "Сервер деканату не відповідає (Timeout або помилка з'єднання)."

    @staticmethod
    def search_global(query: str) -> Dict:
        now = datetime.now()
        if not NungParser._global_cache['teachers'] or not NungParser._global_cache['timestamp'] or (now - NungParser._global_cache['timestamp']).seconds > 3600:
            try:
                t_data = NungParser._fetch_objects('teacher')
                r_data = NungParser._fetch_objects('room')
                if not t_data and not r_data:
                    return {"status": "error", "message": "Сервер деканату не надіслав дані (Timeout/Empty)"}
                NungParser._global_cache['teachers'] = t_data
                NungParser._global_cache['rooms'] = r_data
                NungParser._global_cache['timestamp'] = now
            except requests.exceptions.ConnectionError:
                return {"status": "error", "message": "Відсутній зв'язок з сервером dekanat.nung.edu.ua"}
            except Exception as e:
                return {"status": "error", "message": f"Непередбачена помилка: {str(e)}"}

        query_norm = NungParser._normalize(query)
        results = []
        for t in NungParser._global_cache['teachers']:
            if query_norm in NungParser._normalize(t.get('name', '')):
                results.append(
                    {'type_label': 'Викладач', 'type_code': 't', 'name': t['name'], 'id': t['ID']})
        for r in NungParser._global_cache['rooms']:
            if query_norm in NungParser._normalize(r.get('name', '')):
                results.append(
                    {'type_label': 'Аудиторія', 'type_code': 'r', 'name': r['name'], 'id': r['ID']})
        return {"status": "ok", "data": results[:20]}

    @staticmethod
    def _fetch_objects(req_mode: str) -> List[Dict]:
        for encoding in ['WINDOWS-1251', 'UTF-8']:
            params = {'req_type': 'obj_list', 'req_mode': req_mode, 'show_ID': 'yes',
                      'req_format': 'json', 'coding_mode': encoding, 'bs': 'ok'}
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            try:
                response = requests.get(NungParser.API_URL, params=params, headers=headers, timeout=15, verify=False)
                data = response.json()
                root = data.get('psrozklad_export') or data.get('ps_rozklad_export')
                if not root:
                    continue

                objects = []
                if req_mode == 'teacher':
                    for dept in root.get('departments', []):
                        objects.extend(dept.get('objects', []))
                elif req_mode == 'room':
                    for block in root.get('blocks', []):
                        objects.extend(block.get('objects', []))

                if objects:
                    return objects
            except Exception as e:
                logger.error(f"Fetch error ({encoding}): {e}")
        return []

    @staticmethod
    def _parse_cell_blocks(content_td) -> List[Dict]:
        blocks: List[Dict] = []
        current = None

        def new_block():
            b = {'subject': '', 'subgroup': None, 'teacher': '', 'urls': []}
            blocks.append(b)
            return b

        def add_url(block, url):
            url = url.strip().rstrip('.,;)')
            if url and url not in block['urls']:
                block['urls'].append(url)

        for el in content_td.descendants:
            if not isinstance(el, Tag):
                continue
            classes = el.get('class') or []

            if el.name == 'span' and 'p_name' in classes:
                current = new_block()
                current['subject'] = el.get_text(' ', strip=True)
            elif el.name == 'span' and 'gr2_name' in classes:
                m = re.search(r'підгр\.\s*(\d+)', el.get_text(' ', strip=True), re.IGNORECASE)
                if m and current is not None:
                    current['subgroup'] = m.group(1)
            elif el.name == 'span' and 't_name' in classes:
                if current is not None:
                    current['teacher'] = el.get_text(' ', strip=True)
            elif el.name == 'span' and 'comment' in classes:
                if current is None:
                    current = new_block()
                for url in NungParser._URL_RE.findall(el.get_text(' ', strip=True)):
                    add_url(current, url)
            elif el.name == 'a' and el.get('href'):
                href = el['href'].strip()
                if href.startswith(('http://', 'https://')):
                    if current is None:
                        current = new_block()
                    add_url(current, href)

        return blocks

    @staticmethod
    def _fetch_links_data(group_name: str, start_date: date, end_date: date) -> List[Dict]:
        links_data: List[Dict] = []
        if not group_name:
            return links_data

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://dekanat.nung.edu.ua/cgi-bin/timetable.cgi?n=700'
        }

        try:
            payload = {
                'n': '700', 'faculty': '0', 'teacher': '', 'course': '0',
                'group': group_name.encode('windows-1251'),
                'sdate': start_date.strftime('%d.%m.%Y'),
                'edate': end_date.strftime('%d.%m.%Y')
            }
            response = requests.post(NungParser.HTML_URL, data=payload, headers=headers, timeout=10, verify=False)
            response.encoding = 'windows-1251'
            soup = BeautifulSoup(response.text, 'html.parser')

            current_date = None
            for el in soup.find_all(['h4', 'tr']):
                if el.name == 'h4':
                    m = re.match(r'\s*(\d{2}\.\d{2}\.\d{4})', el.get_text(' ', strip=True))
                    if m:
                        current_date = m.group(1)
                    continue

                if not current_date:
                    continue

                tds = el.find_all('td', recursive=False)
                if len(tds) < 3:
                    continue

                time_match = re.search(r'(\d{2}:\d{2})', tds[1].get_text(' ', strip=True))
                if not time_match:
                    continue

                for block in NungParser._parse_cell_blocks(tds[2]):
                    block['date'] = current_date
                    block['time'] = time_match.group(1)
                    links_data.append(block)

        except Exception as e:
            logger.error(f"HTML Link Parsing Error: {e}")

        return links_data

    @staticmethod
    def _match_links(links_data: List[Dict], start_dt: datetime, subject_text: str,
                     teacher_name: str, subg_num: Optional[str]) -> List[str]:
        d = start_dt.strftime('%d.%m.%Y')
        t = start_dt.strftime('%H:%M')
        candidates = [b for b in links_data if b['date'] == d and b['time'] == t]
        if not candidates:
            return []

        subj_key = NungParser._key(subject_text)
        teacher_key = NungParser._teacher_key(teacher_name)

        scored = []
        for b in candidates:
            score = 0
            if b['subgroup'] and subg_num:
                if b['subgroup'] != subg_num:
                    continue
                score += 4
            b_subj = NungParser._key(b['subject'])
            if b_subj and subj_key and (b_subj in subj_key or subj_key in b_subj):
                score += 3
            b_teacher = NungParser._teacher_key(b['teacher'])
            if teacher_key and b_teacher and teacher_key == b_teacher:
                score += 2
            scored.append((score, b))

        if not scored:
            return []

        best = max(s for s, _ in scored)
        if best == 0 and len(scored) > 1:
            return []

        top = [b for s, b in scored if s == best]
        if len({tuple(b['urls']) for b in top}) > 1:
            return []

        return list(top[0]['urls'])

    @staticmethod
    def get_schedule(obj_id: str, start_date: date = None, end_date: date = None, obj_type: str = 'group', group_name: str = None) -> List[ScheduleEvent]:
        if not start_date:
            start_date = datetime.now(TIMEZONE).date() - timedelta(days=1)
        if not end_date:
            end_date = datetime.now(TIMEZONE).date() + timedelta(days=180)

        links_data = []
        if obj_type == 'group' and group_name:
            links_data = NungParser._fetch_links_data(group_name.lower(), start_date, end_date)

        return NungParser.get_schedule_json(obj_id, obj_type, start_date, end_date, links_data)

    @staticmethod
    def _split_merged_events(description: str) -> List[str]:
        subgroups = list(re.finditer(r'\(підгр\.\s*\d+\)', description))
        if len(subgroups) < 2:
            return [description]
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
                if room_match:
                    split_point = search_start + room_match.end()
                else:
                    teacher_match = re.search(r'[A-ZА-ЯІЇЄ]\.[A-ZА-ЯІЇЄ]\.', segment)
                    if teacher_match:
                        split_point = search_start + teacher_match.end()
                    else:
                        split_point = next_match.start()
                        last_caps = list(re.finditer(r'[A-ZА-ЯІЇЄ][a-zа-яіїє]+', segment))
                        if last_caps:
                            split_point = search_start + last_caps[-1].start()
            else:
                split_point = len(description)
            chunk = description[prev_split:split_point].strip()
            if chunk:
                results.append(chunk)
            prev_split = split_point
        return results
        
    @staticmethod
    def get_schedule_json(obj_id: str, obj_mode: str, start_date: date, end_date: date, links_data: List[Dict] = None) -> List[ScheduleEvent]:
        params = {
            'req_type': 'rozklad', 'req_mode': obj_mode, 'OBJ_ID': obj_id,
            'ros_text': 'separated', 'begin_date': start_date.strftime('%d.%m.%Y'),
            'end_date': end_date.strftime('%d.%m.%Y'), 'req_format': 'json', 'coding_mode': 'UTF8', 'bs': 'ok'
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        try:
            response = requests.get(NungParser.API_URL, params=params, headers=headers, timeout=10, verify=False)
            response.encoding = 'utf-8'
            data = response.json()
            
            if isinstance(data, dict) and 'code' in data:
                err_code = int(data['code'])
                if err_code < 0:
                    err_msg = data.get('error_message', 'Невідома помилка сервера')
                    logger.warning(f"API Деканату повернуло помилку {err_code}: {err_msg}")
                    raise Exception(f"Код {err_code}: {err_msg}")

            events = []
            root = data.get('psrozklad_export') or data.get('ps_rozklad_export')
            items = root.get('roz_items', []) if root else []

            for item in items:
                desc_raw = item.get('lesson_description')
                original_desc = str(desc_raw).strip() if desc_raw else ""

                if not original_desc:
                    title = item.get('title') or ""
                    teacher = item.get('teacher') or ""
                    room = item.get('room') or ""
                    reservation = item.get('reservation') or ""
                    parts = [str(title), str(teacher), str(room), str(reservation)]
                    original_desc = " ".join(p.strip() for p in parts if p.strip())

                descriptions = NungParser._split_merged_events(original_desc)
                json_link = item.get('link') or item.get('url') or ""
                has_multiple_subgroups = len(descriptions) > 1

                for description in descriptions:
                    date_str = item.get('date')
                    time_raw = item.get('lesson_time') or ""
                    time_range = time_raw.split('-')
                    
                    if len(time_range) != 2:
                        continue
                    try:
                        date_obj = datetime.strptime(date_str, '%d.%m.%Y').date()
                        start_time = datetime.strptime(time_range[0].strip(), '%H:%M').time()
                        end_time = datetime.strptime(time_range[1].strip(), '%H:%M').time()
                        start_dt = TIMEZONE.localize(datetime.combine(date_obj, start_time))
                        end_dt = TIMEZONE.localize(datetime.combine(date_obj, end_time))
                    except (ValueError, TypeError):
                        continue

                    room = item.get('room') or ""
                    if not room:
                        room_match = re.search(r'(\d+[^\s]*\.ауд\.)', description)
                        if room_match:
                            room = room_match.group(1)

                    event_type = item.get('type') or ""
                    if not event_type:
                        type_match = re.search(r'\((Л|Пр|Лаб|Л\+Пр|Sem|Екз|Конс)\)', description)
                        if type_match:
                            event_type = type_match.group(1)

                    clean_text = description.replace('*', '').strip()
                    teacher_name = item.get('teacher') or ""
                    base_group = item.get('object') or ""
                    subgroup_info = item.get('group') or ""

                    if obj_mode == 'group':
                        group_name = f"{base_group} {subgroup_info}".strip() if subgroup_info else base_group
                    else:
                        group_name = base_group
                        if not group_name:
                            gm = re.search(r'([A-ZА-ЯІЇЄ]{2,4}-\d{2}-\d)', clean_text)
                            if gm:
                                group_name = gm.group(0)
                        if subgroup_info:
                            group_name = f"{group_name} {subgroup_info}".strip()

                    if not teacher_name and obj_mode == 'group':
                        tm = re.search(r'(доцент|професор|викладач|асистент|зав\.каф\.)\s+[A-ZА-ЯІЇЄ][a-zа-яіїє\']+\s+[A-ZА-ЯІЇЄ][a-zа-яіїє\']+(\s+[A-ZА-ЯІЇЄ][a-zа-яіїє\']+)?', clean_text)
                        if tm:
                            teacher_name = tm.group(0)

                    is_remote = (item.get('online') in ['Tak', 'Yes', '1', 'Так', 'True']) or "дистанційно" in clean_text.lower()
                    clean_text = re.sub(r'(?i)дистанційно', '', clean_text).strip()
                    if not clean_text and item.get('title'):
                        clean_text = item.get('title')

                    is_cancelled = "відмінено" in str(item.get('replacement', '')).lower()
                    
                    subg_match = (re.search(r'підгр\.\s*(\d+)', group_name, re.IGNORECASE)
                                  or re.search(r'підгр\.\s*(\d+)', subgroup_info, re.IGNORECASE)
                                  or re.search(r'підгр\.\s*(\d+)', description, re.IGNORECASE))
                    subg_num = subg_match.group(1) if subg_match else None

                    final_links = []
                    if links_data:
                        final_links = NungParser._match_links(
                            links_data, start_dt,
                            subject_text=original_desc,
                            teacher_name=teacher_name,
                            subg_num=subg_num
                        )

                    if not final_links and json_link and not has_multiple_subgroups:
                        final_links.append(json_link)

                    is_elective_raw = "*(в)" in original_desc.lower() or "військова підготовка" in original_desc.lower()
                    subject_to_save = original_desc if is_elective_raw else (clean_text or original_desc)

                    event_data = {
                        'subject': subject_to_save, 
                        'type': event_type, 'teacher': teacher_name,
                        'room': room, 'group': group_name, 'is_remote': is_remote, 'links': final_links,
                        'start_time': start_dt, 'end_time': end_dt,
                        'is_cancelled': is_cancelled
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
        
        if event.is_unselected:
             subj_line = f"📓 [НЕ ОБРАНО] {event.subject}"
             
        if event.group:
            subj_line += f" {event.group}"
        if event.event_type:
            subj_line += f" ({event.event_type})"
            
        if strikethrough:
            lines.append(f"<s>{subj_line}</s>")
        else:
            if event.is_remote and not event.is_unselected:
                lines.append("💻🏡 <b>ДИСТАНЦІЙНО</b>")
            lines.append(subj_line)
            
            if not event.is_unselected:
                if event.teacher:
                    lines.append(f"👤🎓 {event.teacher}")
                if event.room:
                    lines.append(f"📍 {event.room}")
                if event.links:
                    for link in event.links:
                        link_name = "Посилання на пару 🔗"
                        if "zoom" in link:
                            link_name = "Zoom 🎥"
                        elif "meet.google" in link:
                            link_name = "Google Meet 🎥"
                        elif "teams" in link:
                            link_name = "Teams 🎥"
                        lines.append(f'<a href="{link}">{link_name}</a>')

        return "\n".join(lines)

    @classmethod
    def format_changes(cls, changes: List[ScheduleChange]) -> str:
        if not changes:
            return ""
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
        if len(text) <= max_length:
            return [text]
        parts = []
        while text:
            if len(text) <= max_length:
                parts.append(text)
                break
            split_pos = text.rfind('\n', 0, max_length)
            if split_pos == -1:
                split_pos = max_length
            parts.append(text[:split_pos])
            text = text[split_pos:].lstrip()
        return parts

class ScheduleBot:
    def __init__(self):
        self.formatter = ScheduleFormatter()
        self.user_manager = UserManager()
        self.cache_manager = ScheduleCache()
        self.image_generator = ScheduleImageGenerator(font_path="Roboto-Regular.ttf") if ScheduleImageGenerator else None
        self.application = None
        self._schedule_check_running = False

    @staticmethod
    def _fit_caption(caption: str, limit: int = 1024) -> str:
        def visible(t: str) -> int:
            return len(html.unescape(re.sub(r'<[^>]+>', '', t)))

        if visible(caption) <= limit:
            return caption
        lines = caption.split('\n')
        while lines and visible('\n'.join(lines)) > limit - 3:
            lines.pop()
        return '\n'.join(lines) + '\n…'

    def set_application(self, application):
        self.application = application
        self.application.job_queue.run_daily(self._daily_notification_job, time=DAILY_NOTIFICATION_TIME)
        self.application.job_queue.run_daily(self._weekly_notification_job, time=WEEKLY_NOTIFICATION_TIME, days=[WEEKLY_NOTIFICATION_DAY])
        self.application.job_queue.run_repeating(self._check_schedule_changes_job, interval=SCHEDULE_CHECK_INTERVAL, first=30)

    async def _is_user_admin(self, update: Update) -> bool:
        if update.effective_chat.type == ChatType.PRIVATE:
            return True
        try:
            member = await update.effective_chat.get_member(update.effective_user.id)
            return member.status in [ChatMember.OWNER, ChatMember.ADMINISTRATOR]
        except:
            return False

    async def _get_events(self, group_id: str, context: ContextTypes.DEFAULT_TYPE = None, chat_id: int = None, group_name: str = None, start_date: date = None, end_date: date = None) -> List[ScheduleEvent]:
        debug_info = ""
        try:
            events = await asyncio.to_thread(
                NungParser.get_schedule, 
                group_id, start_date=start_date, end_date=end_date, obj_type='group', group_name=group_name
            )
        except Exception as e:
            logger.error(f"Помилка отримання/парсингу: {e}")
            debug_info = f"❌ Деканат недоступний або повернув помилку:\n<code>{e}</code>"
            events = []
        
        if not events and not debug_info:
            debug_info = "❌ Деканат не повернув розклад (пуста відповідь)."

        if not events:
            logger.warning(f"Сервер не віддав розклад для {group_id}. Використовуємо локальний кеш.")
            cached_events = self.cache_manager._group_caches.get(group_id, [])
            if cached_events and start_date and end_date:
                events = [e for e in cached_events if start_date <= e.start_time.date() <= end_date]
            else:
                events = cached_events
            
            if events:
                debug_info += "\n✅ Розклад успішно піднято з локального кешу (Fallback)."
            else:
                debug_info += "\n⚠️ На жаль, у локальному кеші також немає даних на цей період."

        if context and chat_id and debug_info and ("❌" in debug_info or "⚠️" in debug_info):
            s = self.user_manager.get_user_settings(chat_id)
            if getattr(s, 'debug_mode', False):
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, 
                        text=f"🛠 <b>Системне повідомлення (Дебаг):</b>\n{debug_info}", 
                        parse_mode=ParseMode.HTML,
                        disable_notification=True
                    )
                except Exception as e:
                    logger.error(f"Не вдалося відправити дебаг у чат {chat_id}: {e}")

        return events

    def _apply_elective_filters(self, events: List[ScheduleEvent], chat_id: int):
        s = self.user_manager.get_user_settings(chat_id)
        disabled_list = getattr(s, 'disabled_electives', [])
        
        for e in events:
            if e.is_elective and e.subject in disabled_list:
                e.is_unselected = True

    async def _pin_message_with_management(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
        settings = self.user_manager.get_user_settings(chat_id)
        try:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
            settings.pinned_messages.append(message_id)
            if len(settings.pinned_messages) > MAX_PINNED_MESSAGES:
                oldest_message_id = settings.pinned_messages.pop(0)
                try:
                    await context.bot.unpin_chat_message(chat_id=chat_id, message_id=oldest_message_id)
                except Exception as e:
                    logger.warning(f"Unpin error: {e}")
            self.user_manager.update_user_setting(chat_id, 'pinned_messages', settings.pinned_messages)
        except Exception as e:
            logger.error(f"Pin error: {e}")

    async def _weekly_notification_job(self, context: ContextTypes.DEFAULT_TYPE):
        users = [uid for uid, s in self.user_manager.users.items() if s.weekly_notifications]
        tomorrow = datetime.now(TIMEZONE).date() + timedelta(days=1)
        for chat_id in users:
            s = self.user_manager.get_user_settings(chat_id)
            if not s.group_id:
                continue
            
            events = await self._get_events(s.group_id, context=context, chat_id=chat_id, group_name=s.group_name, start_date=tomorrow, end_date=tomorrow + timedelta(days=6))
            if not events:
                continue
                
            self._apply_elective_filters(events, chat_id)
            
            if self.image_generator:
                photo_bio = self.image_generator.create_week_image(events, tomorrow, theme=getattr(s, 'theme', 'light'))
                try:
                    msg = await context.bot.send_photo(chat_id=chat_id, photo=photo_bio, caption=f"📅 Тиждень: {s.group_name}")
                    await self._pin_message_with_management(context, chat_id, msg.message_id)
                except Exception as e:
                    logger.error(f"Weekly error: {e}")

    async def _daily_notification_job(self, context: ContextTypes.DEFAULT_TYPE):
        today = datetime.now(TIMEZONE).date()
        users = [uid for uid, s in self.user_manager.users.items() if s.daily_notifications]
        for chat_id in users:
            s = self.user_manager.get_user_settings(chat_id)
            if not s.group_id:
                continue

            events = await self._get_events(s.group_id, context=context, chat_id=chat_id, group_name=s.group_name, start_date=today, end_date=today)
            if not events:
                continue

            self._apply_elective_filters(events, chat_id)

            if self.image_generator:
                photo_bio = self.image_generator.create_day_image(events, today, theme=getattr(s, 'theme', 'light'))

                subject_links = {}
                for e in events:
                    if not e.links or getattr(e, 'is_unselected', False):
                        continue
                    for link in e.links:
                        key = (e.subject, e.group)
                        if key not in subject_links:
                            subject_links[key] = {}
                        if link not in subject_links[key]:
                            subject_links[key][link] = []
                        subject_links[key][link].append(e.start_time)

                time_grouped = {}
                for (subject, group), links_data in subject_links.items():
                    for link, times in links_data.items():
                        for start_time in times:
                            time_key = start_time.strftime("%H:%M")
                            if time_key not in time_grouped:
                                time_grouped[time_key] = []
                            time_grouped[time_key].append({'subject': subject, 'group': group, 'link': link, 'start_time': start_time})

                sorted_times = sorted(time_grouped.keys())

                links_text_lines = []
                for time_key in sorted_times:
                    items = time_grouped[time_key]
                    pair_num = get_pair_number(items[0]['start_time'])
                    pair_emoji = PAIR_EMOJIS.get(pair_num, "📚")

                    time_subject_count = {}
                    for item in items:
                        subj = item['subject']
                        time_subject_count[subj] = time_subject_count.get(subj, 0) + 1

                    for idx, item in enumerate(items):
                        subject = item['subject']
                        group = item['group']
                        link = item['link']

                        subject_display = subject
                        if time_subject_count[subject] > 1 and group:
                            subject_display = f"{subject} {group}"

                        link_name = "Meet 🎥" if "meet" in link else ("Zoom 🎥" if "zoom" in link else "🔗")
                        if idx == 0:
                            links_text_lines.append(f"{pair_emoji} {subject_display}: <a href=\"{link}\">{link_name}</a>")
                        else:
                            links_text_lines.append(f"{'   '} {subject_display}: <a href=\"{link}\">{link_name}</a>")

                caption = f"📅 Сьогодні: {s.group_name}"
                if links_text_lines:
                    caption += "\n\n🔗 <b>Посилання на пари:</b>\n" + "\n".join(links_text_lines)

                other_links = []
                for e in events:
                    if not e.links or getattr(e, 'is_unselected', False):
                        continue
                    for link in e.links:
                        if any(x in link.lower() for x in ['zoom.us', 'meet.google', 'teams.microsoft', 'webex']):
                            continue
                        pair_num = get_pair_number(e.start_time)
                        pair_emoji = PAIR_EMOJIS.get(pair_num, "📎")
                        subject_short = e.subject[:30] + "..." if len(e.subject) > 30 else e.subject
                        if e.group and len(events) > 1:
                            subject_short = f"{subject_short} {e.group}"
                        other_links.append(f"{pair_emoji} {subject_short}: <a href=\"{link}\">📄 Матеріали</a>")

                if other_links:
                    caption += "\n\n📚 <b>Додаткові матеріали:</b>\n" + "\n".join(other_links)

                caption = self._fit_caption(caption)

                try:
                    msg = await context.bot.send_photo(chat_id=chat_id, photo=photo_bio, caption=caption, parse_mode=ParseMode.HTML)
                    await self._pin_message_with_management(context, chat_id, msg.message_id)
                except Exception as e:
                    logger.error(f"Daily error: {e}")

    async def _check_schedule_changes_job(self, context: ContextTypes.DEFAULT_TYPE):
        if self._schedule_check_running:
            return
        self._schedule_check_running = True
        try:
            active_groups = {}
            for s in self.user_manager.users.values():
                if s.group_id and s.change_notifications:
                    active_groups[s.group_id] = s.group_name

            for group_id, group_name in active_groups.items():
                new_events = await self._get_events(group_id, group_name=group_name)

                if not new_events:
                    old_events = self.cache_manager._group_caches.get(group_id, [])
                    if old_events:
                        continue

                changes = self.cache_manager.update_and_detect_changes(group_id, new_events)
                if changes:
                    text_changes = self.formatter.format_changes(changes)
                    if not text_changes.strip():
                        continue
                    targets = [uid for uid, s in self.user_manager.users.items() if s.group_id == group_id and s.change_notifications]
                    for chat_id in targets:
                        try:
                            msg = await context.bot.send_message(chat_id=chat_id, text=text_changes, parse_mode=ParseMode.HTML, disable_notification=True)
                            await self._pin_message_with_management(context, chat_id, msg.message_id)
                        except:
                            pass
        except Exception as e:
            logger.error(f"Check job error: {e}")
        finally:
            self._schedule_check_running = False

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = self.user_manager.get_user_settings(update.effective_chat.id)
        msg = f"👋 Привіт!\n"
        if s.group_name:
            msg += f"✅ Група: <b>{s.group_name}</b>\n"
        else:
            msg += "⚠️ Напишіть: <code>/group Назва</code>\n"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=self.get_main_keyboard(), disable_notification=True)

    async def group_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_user_admin(update):
            return await update.message.reply_text("⛔ Тільки адміністратори чату можуть змінювати групу.")
        if not context.args:
            return await update.message.reply_text("❌ Приклад: `/group КІ-24-1`", parse_mode=ParseMode.MARKDOWN)
            
        group_name = " ".join(context.args)
        group_id, error_message = NungParser.get_group_id(group_name)
        
        if group_id:
            self.user_manager.update_user_group(update.effective_chat.id, group_name.upper(), group_id)
            events = await self._get_events(group_id, context=context, chat_id=update.effective_chat.id, group_name=group_name.upper())
            self.cache_manager.update_and_detect_changes(group_id, events)
            await update.message.reply_text(f"✅ Збережено: <b>{group_name.upper()}</b>", parse_mode=ParseMode.HTML, reply_markup=self.get_main_keyboard())
        else:
            await update.message.reply_text(f"❌ <b>Помилка:</b> {error_message}", parse_mode=ParseMode.HTML)

    async def _send_schedule_image(self, update: Update, events: List[ScheduleEvent], date_obj: date, mode: str, caption: str):
        if not self.image_generator:
            text_response = caption + "\n\n"
            for e in events:
                text_response += self.formatter._build_event_details(e) + "\n\n"
            
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Меню", callback_data="back")]
            ])
            
            if update.callback_query:
                try:
                    await update.callback_query.message.edit_text(text_response, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb)
                except Exception as e:
                    if "Message is not modified" not in str(e):
                        await update.effective_chat.send_message(text_response, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb)
            else:
                await update.effective_chat.send_message(text_response, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb)
            return

        s = self.user_manager.get_user_settings(update.effective_chat.id)
        current_theme = getattr(s, 'theme', 'light')

        if mode == 'week':
            bio = self.image_generator.create_week_image(events, date_obj, theme=current_theme)
        else:
            bio = self.image_generator.create_day_image(events, date_obj, theme=current_theme)

        subject_links = {}
        for e in events:
            if not e.links or getattr(e, 'is_unselected', False):
                continue
            for link in e.links:
                key = (e.subject, e.group)
                if key not in subject_links:
                    subject_links[key] = {}
                if link not in subject_links[key]:
                    subject_links[key][link] = []
                subject_links[key][link].append(e.start_time)

        time_grouped = {}
        for (subject, group), links_data in subject_links.items():
            for link, times in links_data.items():
                for start_time in times:
                    time_key = start_time.strftime("%d.%m %H:%M")
                    if time_key not in time_grouped:
                        time_grouped[time_key] = []
                    time_grouped[time_key].append({'subject': subject, 'group': group, 'link': link, 'start_time': start_time})

        sorted_times = sorted(time_grouped.keys())

        links_text_lines = []
        for time_key in sorted_times:
            items = time_grouped[time_key]
            pair_num = get_pair_number(items[0]['start_time'])
            pair_emoji = PAIR_EMOJIS.get(pair_num, "📚")

            time_subject_count = {}
            for item in items:
                subj = item['subject']
                time_subject_count[subj] = time_subject_count.get(subj, 0) + 1

            for idx, item in enumerate(items):
                subject = item['subject']
                group = item['group']
                link = item['link']

                subject_display = subject
                if time_subject_count[subject] > 1 and group:
                    subject_display = f"{subject} {group}"

                link_name = "Meet 🎥" if "meet" in link else ("Zoom 🎥" if "zoom" in link else "🔗")
                if idx == 0:
                    links_text_lines.append(f"{pair_emoji} {subject_display}: <a href=\"{link}\">{link_name}</a>")
                else:
                    links_text_lines.append(f"{'   '} {subject_display}: <a href=\"{link}\">{link_name}</a>")

        full_caption = caption
        if links_text_lines:
            full_caption += "\n\n🔗 <b>Посилання на пари:</b>\n" + "\n".join(links_text_lines)

        other_links = []
        for e in events:
            if not e.links or getattr(e, 'is_unselected', False):
                continue
            for link in e.links:
                if any(x in link.lower() for x in ['zoom.us', 'meet.google', 'teams.microsoft', 'webex']):
                    continue
                pair_num = get_pair_number(e.start_time)
                pair_emoji = PAIR_EMOJIS.get(pair_num, "📎")
                subject_short = e.subject[:30] + "..." if len(e.subject) > 30 else e.subject
                if e.group and len(events) > 1:
                    subject_short = f"{subject_short} {e.group}"
                other_links.append(f"{pair_emoji} {subject_short}: <a href=\"{link}\">📄 Матеріали</a>")

        if other_links:
            full_caption += "\n\n📚 <b>Додаткові матеріали:</b>\n" + "\n".join(other_links)

        full_caption = self._fit_caption(full_caption)

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
                try:
                    await update.callback_query.edit_message_media(media=media, reply_markup=kb)
                except Exception as e:
                    logger.warning(f"Edit media warning: {e}")
            else:
                await update.callback_query.message.delete()
                await update.effective_chat.send_photo(photo=bio, caption=full_caption, reply_markup=kb, parse_mode=ParseMode.HTML, disable_notification=True)
        else:
            await update.effective_chat.send_photo(photo=bio, caption=full_caption, reply_markup=kb, parse_mode=ParseMode.HTML, disable_notification=True)
            
    async def _generic_schedule_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, mode='today', target_date=None):
        s = self.user_manager.get_user_settings(update.effective_chat.id)
        if not s.group_id:
            return await update.effective_message.reply_text("⚠️ Оберіть групу: `/group Назва`")

        now = datetime.now(TIMEZONE).date()
        if not target_date:
            target_date = now if mode == 'today' else (now + timedelta(days=1))
            if mode == 'week':
                target_date = now

        if mode == 'week':
            target_date = target_date - timedelta(days=target_date.weekday())
            fetch_start = target_date
            fetch_end = target_date + timedelta(days=6)
        else:
            fetch_start = target_date
            fetch_end = target_date

        events = await self._get_events(s.group_id, context=context, chat_id=update.effective_chat.id, group_name=s.group_name, start_date=fetch_start, end_date=fetch_end)
        
        self._apply_elective_filters(events, update.effective_chat.id)

        if mode == 'week':
            filtered_events = [e for e in events if target_date <= e.start_time.date() <= target_date + timedelta(days=6)]
            caption = f"📅 Розклад: {s.group_name}"
        else:
            filtered_events = [e for e in events if e.start_time.date() == target_date]
            caption = f"📅 {target_date.strftime('%d.%m')} - {s.group_name}"

        await self._send_schedule_image(update, filtered_events, target_date, mode, caption)

    async def today_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._generic_schedule_command(update, context, 'today')

    async def tomorrow_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._generic_schedule_command(update, context, 'tomorrow')

    async def week_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._generic_schedule_command(update, context, 'week')

    async def date_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            return await update.message.reply_text("📅 Формат: `/date 19.12`", parse_mode=ParseMode.MARKDOWN)
        try:
            day, month = map(int, context.args[0].split('.'))
            target_date = date(datetime.now(TIMEZONE).year, month, day)
        except ValueError:
            return await update.message.reply_text("❌ Невірний формат. Приклад: `/date 19.12`", parse_mode=ParseMode.MARKDOWN)

        await self._generic_schedule_command(update, context, 'date', target_date)

    async def electives_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = self.user_manager.get_user_settings(update.effective_chat.id)
        if not s.group_id:
            msg = "⚠️ Спочатку оберіть групу: /group"
            if update.callback_query:
                return await update.callback_query.message.reply_text(msg)
            return await update.message.reply_text(msg)

        now = datetime.now(TIMEZONE).date()
        events = await self._get_events(s.group_id, context=context, chat_id=update.effective_chat.id, group_name=s.group_name, start_date=now, end_date=now + timedelta(days=180))
        
        electives = set()
        for e in events:
            if e.is_elective:
                electives.add(e.subject)

        if not electives:
            msg = "📚 У вашій групі не знайдено предметів з *(в) на найближчий семестр."
            if update.callback_query:
                return await update.callback_query.message.reply_text(msg)
            return await update.message.reply_text(msg)

        keyboard = []
        disabled_list = getattr(s, 'disabled_electives', [])
        
        for el in sorted(electives):
            el_hash = hashlib.md5(el.encode()).hexdigest()[:10]
            is_disabled = el in disabled_list
            status = "❌" if is_disabled else "✅"
            keyboard.append([InlineKeyboardButton(f"{status} {el}", callback_data=f"toggle_el|{el_hash}")])
            
        keyboard.append([InlineKeyboardButton("◀️ Головне меню", callback_data="back")])
        text = "⚙️ <b>Керування вибірковими:</b>\n<i>Натисніть на предмет, щоб увімкнути (✅) або вимкнути (❌) його відображення:</i>"
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
            except Exception:
                await update.callback_query.message.delete()
                await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML, disable_notification=True)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    async def search_all_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            return await update.message.reply_text("🔍 Приклад: `/search_all Коваль`")

        result = await asyncio.to_thread(NungParser.search_global, " ".join(context.args))

        if result.get("status") == "error":
            error_msg = f"❌ <b>Помилка пошуку</b>\n\n⚠️ Суть: <code>{result['message']}</code>\n\n<i>Спробуйте пізніше.</i>"
            return await update.message.reply_text(error_msg, parse_mode=ParseMode.HTML)

        results = result.get("data", [])
        if not results:
            return await update.message.reply_text("❌ Нічого не знайдено.")

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
        if not s.group_id:
            return await update.message.reply_text("⚠️ Оберіть групу.")
        if not context.args:
            return await update.message.reply_text("🔍 Приклад: `/search Математика`", parse_mode=ParseMode.MARKDOWN)

        query = " ".join(context.args)
        events = await self._get_events(s.group_id, context=context, chat_id=update.effective_chat.id, group_name=s.group_name)
        found = [e for e in events if e.matches_query(query) and e.start_time.date() >= datetime.now(TIMEZONE).date()]
        if not found:
            return await update.message.reply_text("📭 Нічого не знайдено.")

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
            theme_icon = "☀️ Світла" if getattr(s, 'theme', 'light') == 'light' else "🌙 Темна"
            kb_rows.append([InlineKeyboardButton(f"Тема: {theme_icon}", callback_data="toggle_theme")])
            kb_rows.append([InlineKeyboardButton(f"Сповіщення про зміни {'✅' if s.change_notifications else '❌'}", callback_data="toggle_changes")])
            kb_rows.append([InlineKeyboardButton(f"Щоденно о {DAILY_NOTIFICATION_TIME} {'✅' if s.daily_notifications else '❌'}", callback_data="toggle_daily")])
            kb_rows.append([InlineKeyboardButton(f"Розклад на тиждень о {WEEKLY_NOTIFICATION_TIME} {'✅' if s.weekly_notifications else '❌'}", callback_data="toggle_weekly")])
            kb_rows.append([InlineKeyboardButton(f"Режим дебагу 🛠️ {'✅' if getattr(s, 'debug_mode', False) else '❌'}", callback_data="toggle_debug")])

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
            [InlineKeyboardButton("📊 Тиждень", callback_data="week"), InlineKeyboardButton("📚 Вибіркові", callback_data="electives")],
            [InlineKeyboardButton("⚙️ Меню", callback_data="notifications")]
        ])

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data

        try:
            await query.answer()
        except:
            pass

        if data == "delete_msg":
            await query.message.delete()
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

        elif data in ['today', 'tomorrow', 'week']:
            await self._generic_schedule_command(update, context, data)
        elif data == "notifications":
            await self.notifications_command(update, context)
        elif data == "electives":
            await self.electives_command(update, context)

        elif data.startswith("toggle_el|"):
            if not await self._is_user_admin(update):
                return await query.answer("⛔ Тільки адміністратори можуть змінювати налаштування!", show_alert=True)
                
            el_hash = data.split("|")[1]
            s = self.user_manager.get_user_settings(update.effective_chat.id)
            
            now = datetime.now(TIMEZONE).date()
            events = await self._get_events(s.group_id, context=context, chat_id=update.effective_chat.id, group_name=s.group_name, start_date=now, end_date=now + timedelta(days=180))
            target_subject = next((e.subject for e in events if e.is_elective and hashlib.md5(e.subject.encode()).hexdigest()[:10] == el_hash), None)
            
            if target_subject:
                disabled_list = getattr(s, 'disabled_electives', [])
                
                if target_subject in disabled_list:
                    disabled_list.remove(target_subject)
                else:
                    disabled_list.append(target_subject)
                
                self.user_manager.update_user_setting(update.effective_chat.id, 'disabled_electives', disabled_list)
                setattr(s, 'disabled_electives', disabled_list)
                
                await self.electives_command(update, context)

        elif data.startswith("toggle_"):
            if not await self._is_user_admin(update):
                return await query.answer("⛔ Тільки адміністратори можуть змінювати налаштування!", show_alert=True)
            
            if data == "toggle_theme":
                s = self.user_manager.get_user_settings(update.effective_chat.id)
                new_theme = "dark" if getattr(s, 'theme', 'light') == "light" else "light"
                self.user_manager.update_user_setting(update.effective_chat.id, 'theme', new_theme)
                await self.notifications_command(update, context)
                return

            if data == "toggle_changes":
                setting = "change_notifications"
            elif data == "toggle_daily":
                setting = "daily_notifications"
            elif data == "toggle_weekly":
                setting = "weekly_notifications"
            elif data == "toggle_debug":
                setting = "debug_mode"
            else:
                return

            curr = getattr(self.user_manager.get_user_settings(update.effective_chat.id), setting, False)
            self.user_manager.update_user_setting(update.effective_chat.id, setting, not curr)
            await self.notifications_command(update, context)

        elif data.startswith("sched|"):
            parts = data.split("|")
            mode, date_str = parts[1], parts[2]
            target_date = datetime.now(TIMEZONE).date() if date_str == "today" else datetime.strptime(date_str, "%Y-%m-%d").date()
            await self._generic_schedule_command(update, context, mode, target_date)

        elif data.startswith("view_sched_img|"):
            parts = data.split("|")
            type_code, obj_id, date_str = parts[1], parts[2], parts[3]
            target_date = datetime.now(TIMEZONE).date() if date_str == "today" else datetime.strptime(date_str, "%Y-%m-%d").date()
            obj_mode = 'teacher' if type_code == 't' else 'room'

            events = await asyncio.to_thread(NungParser.get_schedule, obj_id, start_date=target_date, end_date=target_date, obj_type=obj_mode)

            s = self.user_manager.get_user_settings(update.effective_chat.id)
            current_theme = getattr(s, 'theme', 'light')

            if self.image_generator:
                bio = self.image_generator.create_day_image(events, target_date, theme=current_theme)
                prev_date = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
                next_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️", callback_data=f"view_sched_img|{type_code}|{obj_id}|{prev_date}"),
                     InlineKeyboardButton("Сьогодні", callback_data=f"view_sched_img|{type_code}|{obj_id}|today"),
                     InlineKeyboardButton("➡️", callback_data=f"view_sched_img|{type_code}|{obj_id}|{next_date}")],
                    [InlineKeyboardButton("❌ Закрити", callback_data="delete_msg")]
                ])

                if query.message.photo:
                    await query.edit_message_media(media=InputMediaPhoto(bio, caption=f"Розклад: {obj_id}"), reply_markup=kb)
                else:
                    await query.message.delete()
                    await query.message.chat.send_photo(photo=bio, caption=f"Розклад: {obj_id}", reply_markup=kb, disable_notification=True)
                    
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    
    error_msg = f"🚨 <b>КРАШ БОТА!</b> 🚨\n\n<pre><code class='language-python'>{html.escape(tb_string[-3000:])}</code></pre>"
    
    if isinstance(update, Update) and update.effective_chat:
        try:
            if update.callback_query:
                await update.callback_query.answer()
            await context.bot.send_message(chat_id=update.effective_chat.id, text=error_msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Не зміг відправити помилку: {e}")

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing")
        return
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
    application.add_handler(CommandHandler("electives", bot.electives_command))

    application.add_handler(CallbackQueryHandler(bot.button_callback))
    application.add_error_handler(global_error_handler)
    logger.info("Bot started...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()

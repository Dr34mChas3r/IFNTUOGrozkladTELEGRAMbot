import textwrap
from datetime import datetime, timedelta
from io import BytesIO
from collections import defaultdict
from PIL import Image, ImageDraw, ImageFont
import qrcode

class ScheduleImageGenerator:
    def __init__(self, font_path="/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Regular.ttf"):
        self.BG_COLOR = "#F0F2F5"
        self.TEXT_MAIN = "#000000"
        self.TEXT_SEC = "#555555"
        self.TIME_BG = "#E1E8ED"
        self.CARD_BG = "#FFFFFF"
        
        # Насичена палітра кольорів
        self.ACCENT_BLUE = "#0000ff"   # Насичений синій
        self.ACCENT_ORANGE = "#ffa500" # Насичений помаранчевий
        self.ACCENT_GREEN = "#008000"  # Насичений зелений
        self.ACCENT_RED = "#d60000"    # Глибокий червоний (для відміни)

        self.WIDTH = 1200
        self.PADDING = 40
        self.QR_SIZE = 130
        self.CARD_PADDING = 25 
        
        # Фіксована ширина картки
        self.FIXED_CARD_WIDTH = self.WIDTH - 210 
        
        try:
            self.font_header = ImageFont.truetype(font_path, 42)
            self.font_time = ImageFont.truetype(font_path, 34)
            self.font_subject = ImageFont.truetype(font_path, 36)
            self.font_details = ImageFont.truetype(font_path, 28)
            self.font_status = ImageFont.truetype(font_path, 24)
        except OSError:
            self.font_header = ImageFont.load_default()
            self.font_time = ImageFont.load_default()
            self.font_subject = ImageFont.load_default()
            self.font_details = ImageFont.load_default()
            self.font_status = ImageFont.load_default()

    def _wrap_text(self, text, font, max_px_width):
        """Розбиває текст на рядки відповідно до заданої ширини"""
        if not text: return []
        avg_char = font.getlength("a") if hasattr(font, 'getlength') else 15
        max_chars = max(1, int(max_px_width / avg_char))
        
        lines = []
        for paragraph in text.split('\n'):
            lines.extend(textwrap.wrap(paragraph, width=max_chars))
        return lines

    def _prepare_event_content(self, event):
        """Обчислює висоту контенту та готує дані для малювання"""
        is_cancelled = getattr(event, 'is_cancelled', False)
        has_qr = bool(event.links) and not is_cancelled
        
        qr_space = (self.QR_SIZE + 30) if has_qr else 0
        content_max_w = self.FIXED_CARD_WIDTH - qr_space - (self.CARD_PADDING * 2) - 50

        # Перевіряємо підгрупи
        subj_raw = event.subject
        grp_raw = event.group if event.group else ""
        has_sg1 = "(підгр. 1)" in subj_raw or "(підгр. 1)" in grp_raw
        has_sg2 = "(підгр. 2)" in subj_raw or "(підгр. 2)" in grp_raw
        
        # Висота плашок (вони тепер в один ряд, тому додаємо висоту лише раз)
        badge_h = 45 if (is_cancelled or has_sg1 or has_sg2) else 0
        
        display_subj = event.subject
        if event.group and "(підгр." not in display_subj.lower():
            display_subj += f" {event.group}"
            
        subj_lines = self._wrap_text(display_subj, self.font_subject, content_max_w)
        subj_h = len(subj_lines) * 48

        meta = []
        if event.event_type: meta.append(f"({event.event_type})")
        if event.room: meta.append(f"Ауд: {event.room}")
        if event.is_remote: meta.append("Online")
        meta_lines = self._wrap_text(" | ".join(meta), self.font_details, content_max_w)
        meta_h = len(meta_lines) * 38

        teacher_lines = self._wrap_text(f"Викл: {event.teacher}" if event.teacher else "", self.font_details, content_max_w)
        teacher_h = len(teacher_lines) * 38

        total_h = self.CARD_PADDING * 2 + badge_h + subj_h + meta_h + teacher_h + 15
        min_h = (self.QR_SIZE + self.CARD_PADDING * 2) if has_qr else 120
        
        return {
            'lines': { 'subj': subj_lines, 'meta': meta_lines, 'teacher': teacher_lines },
            'height': max(min_h, total_h),
            'has_qr': has_qr,
            'is_cancelled': is_cancelled,
            'has_sg1': has_sg1,
            'has_sg2': has_sg2,
            'subj_lines': subj_lines
        }

    def _draw_event_card(self, img, draw, x, y, event):
        data = self._prepare_event_content(event)
        
        card_x1 = x + 130
        card_x2 = card_x1 + self.FIXED_CARD_WIDTH
        card_h = data['height']
        
        # Визначаємо колір смужки акценту
        bar_color = self.ACCENT_GREEN
        if data['has_sg1']: bar_color = self.ACCENT_BLUE
        elif data['has_sg2']: bar_color = self.ACCENT_ORANGE
        
        # Смужка червона лише якщо це загальна відміна (без прив'язки до підгрупи)
        if data['is_cancelled'] and not (data['has_sg1'] or data['has_sg2']):
            bar_color = self.ACCENT_RED

        # Малюємо картку
        draw.rounded_rectangle([card_x1 + 3, y + 3, card_x2 + 3, y + card_h + 3], radius=15, fill="#00000008")
        draw.rounded_rectangle([card_x1, y, card_x2, y + card_h], radius=15, fill=self.CARD_BG)
        
        # Смужка акценту зліва
        draw.rounded_rectangle([card_x1 + 10, y + 15, card_x1 + 18, y + card_h - 15], radius=5, fill=bar_color)
        
        curr_y = y + self.CARD_PADDING
        badge_x = card_x1 + 35

        # --- МАЛЮЄМО ПЛАШКИ В РЯД ---
        badge_added = False
        
        # 1. Плашка підгрупи
        if data['has_sg1']:
            draw.rounded_rectangle([badge_x, curr_y, badge_x + 165, curr_y + 35], radius=8, fill=self.ACCENT_BLUE)
            draw.text((badge_x + 15, curr_y + 4), "Підгрупа 1", font=self.font_status, fill="white")
            badge_x += 180
            badge_added = True
        elif data['has_sg2']:
            draw.rounded_rectangle([badge_x, curr_y, badge_x + 165, curr_y + 35], radius=8, fill=self.ACCENT_ORANGE)
            draw.text((badge_x + 15, curr_y + 4), "Підгрупа 2", font=self.font_status, fill=self.TEXT_MAIN)
            badge_x += 180
            badge_added = True

        # 2. Плашка ВІДМІНЕНО (в тому ж ряду)
        if data['is_cancelled']:
            draw.rounded_rectangle([badge_x, curr_y, badge_x + 165, curr_y + 35], radius=8, fill=self.ACCENT_RED)
            draw.text((badge_x + 15, curr_y + 4), "ВІДМІНЕНО", font=self.font_status, fill="white")
            badge_x += 180
            badge_added = True

        # Якщо була хоч одна плашка, пересуваємо курсор Y нижче для тексту предмета
        if badge_added:
            curr_y += 45

        if data['has_qr']:
            try:
                qr = qrcode.QRCode(box_size=2, border=1)
                qr.add_data(event.links[0]); qr.make(fit=True)
                qr_img = qr.make_image().resize((self.QR_SIZE, self.QR_SIZE))
                img.paste(qr_img, (int(card_x2 - self.QR_SIZE - 20), int(y + self.CARD_PADDING)))
            except: pass

        # Малюємо основний текст предмета
        subj_x = card_x1 + 35
        for line in data['subj_lines']:
            draw.text((subj_x, curr_y), line, font=self.font_subject, fill=self.TEXT_MAIN)
            curr_y += 48
        
        # Деталі та викладач
        for line in data['lines']['meta']:
            draw.text((subj_x, curr_y + 5), line, font=self.font_details, fill=self.TEXT_SEC)
            curr_y += 38

        for line in data['lines']['teacher']:
            draw.text((subj_x, curr_y + 5), line, font=self.font_details, fill=self.TEXT_SEC)
            curr_y += 38

        return card_h

    def _draw_time_column(self, draw, x, y, h, start_time, end_time):
        draw.rounded_rectangle([x, y, x + 110, y + h], radius=15, fill=self.TIME_BG)
        draw.text((x + 15, y + 20), start_time.strftime('%H:%M'), font=self.font_time, fill=self.TEXT_MAIN)
        draw.text((x + 15, y + 60), end_time.strftime('%H:%M'), font=self.font_time, fill=self.TEXT_SEC)

    def create_day_image(self, events, date_obj) -> BytesIO:
        events.sort(key=lambda x: x.start_time)
        grouped = defaultdict(list)
        for e in events: grouped[(e.start_time, e.end_time)].append(e)
            
        sorted_keys = sorted(grouped.keys())
        total_h = 150
        slot_data = {}
        
        for key in sorted_keys:
            group = grouped[key]
            h_acc = 0
            for i, ev in enumerate(group):
                prep = self._prepare_event_content(ev)
                h_acc += prep['height']
                if i < len(group)-1: h_acc += 20
            slot_data[key] = h_acc
            total_h += h_acc + 40
            
        img = Image.new('RGB', (self.WIDTH, max(400, total_h)), color=self.BG_COLOR)
        draw = ImageDraw.Draw(img)
        
        header = f"{date_obj.strftime('%d.%m.%Y')} ({['Понеділок','Вівторок','Середа','Четвер','П’ятниця','Субота','Неділя'][date_obj.weekday()]})"
        draw.text((self.PADDING, self.PADDING), header, font=self.font_header, fill=self.TEXT_MAIN)
        
        cursor_y = 130
        if not events:
            draw.text((self.PADDING, cursor_y), "Пар немає, можна відпочивати!", font=self.font_subject, fill=self.TEXT_SEC)
        else:
            for key in sorted_keys:
                h = slot_data[key]
                self._draw_time_column(draw, self.PADDING, cursor_y, h, key[0], key[1])
                sub_y = cursor_y
                for i, ev in enumerate(grouped[key]):
                    sub_y += self._draw_event_card(img, draw, self.PADDING, sub_y, ev)
                    if i < len(grouped[key])-1: sub_y += 20
                cursor_y += h + 40

        bio = BytesIO()
        img.crop((0, 0, self.WIDTH, cursor_y + 20)).save(bio, 'PNG')
        bio.seek(0)
        return bio

    def create_week_image(self, events, start_date) -> BytesIO:
        imgs = []
        total_h = 180
        for i in range(7):
            d = start_date + timedelta(days=i)
            day_evs = [e for e in events if e.start_time.date() == d]
            if not day_evs and i >= 5: continue
            day_img = Image.open(self.create_day_image(day_evs, d))
            imgs.append(day_img)
            total_h += day_img.height + 30

        final = Image.new('RGB', (self.WIDTH, total_h), color=self.BG_COLOR)
        draw = ImageDraw.Draw(final)
        draw.text((self.PADDING, 40), f"Тиждень: {start_date.strftime('%d.%m')} - {(start_date+timedelta(days=6)).strftime('%d.%m')}", font=self.font_header, fill=self.TEXT_MAIN)
        
        curr_y = 140
        for im in imgs:
            final.paste(im, (0, curr_y))
            curr_y += im.height + 30
            
        bio = BytesIO()
        final.save(bio, 'PNG')
        bio.seek(0)
        return bio

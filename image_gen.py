import textwrap
from datetime import datetime, timedelta
from io import BytesIO
from collections import defaultdict
from PIL import Image, ImageDraw, ImageFont
import qrcode  # pip install qrcode[pil]

class ScheduleImageGenerator:
    def __init__(self, font_path="/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Regular.ttf"):
        self.BG_COLOR = "#F0F2F5"
        self.TEXT_MAIN = "#000000"
        self.TEXT_SEC = "#555555"
        self.TIME_BG = "#b7c4c4"
        self.CARD_BG = "#FFFFFF"
        
        self.ACCENT_BLUE = "#3498db"
        self.ACCENT_ORANGE = "#e67e22"
        self.ACCENT_GRAY = "#95a5a6"

        self.WIDTH = 1200
        self.PADDING = 40
        self.QR_SIZE = 150
        self.CARD_PADDING = 20  # Збільшено внутрішній відступ
        
        try:
            self.font_header = ImageFont.truetype(font_path, 42)
            self.font_time = ImageFont.truetype(font_path, 34)
            self.font_subject = ImageFont.truetype(font_path, 34)
            self.font_details = ImageFont.truetype(font_path, 26)
        except OSError:
            self.font_header = ImageFont.load_default()
            self.font_time = ImageFont.load_default()
            self.font_subject = ImageFont.load_default()
            self.font_details = ImageFont.load_default()

    def _wrap_text(self, text, font, max_width):
        lines = []
        if not text: return lines
        try:
            # Використовуємо середню ширину символа для приблизного розрахунку
            avg_char_width = font.getlength("a") if hasattr(font, 'getlength') else font.getsize("a")[0]
        except Exception:
            avg_char_width = 10
            
        if avg_char_width == 0: avg_char_width = 10
        max_chars = int(max_width / avg_char_width)
        for paragraph in text.split('\n'):
            lines.extend(textwrap.wrap(paragraph, width=max_chars))
        return lines

    def _calculate_event_block_height(self, event, width):
        has_qr = bool(event.links)
        qr_reserved_space = (self.QR_SIZE + 40) if has_qr else 40
        
        # Відступ зліва (130 для часу + 40 для смужки/тексту)
        max_text_width = width - 130 - 40 - qr_reserved_space
        
        display_subject = event.subject
        if "(підгр." not in display_subject.lower() and event.group:
             display_subject += f" {event.group}"

        subject_lines = self._wrap_text(display_subject, self.font_subject, max_text_width)
        height = (len(subject_lines) * 45) + 15
        
        meta_info = []
        if event.event_type: meta_info.append(f"({event.event_type})")
        if event.room: meta_info.append(f"Ауд: {event.room}")
        if event.is_remote: meta_info.append("Online")
        
        details_text = " | ".join(meta_info)
        details_lines = self._wrap_text(details_text, self.font_details, max_text_width)
        if event.teacher:
            teacher_lines = self._wrap_text(f"Викл: {event.teacher}", self.font_details, max_text_width)
            details_lines.extend(teacher_lines)
            
        height += (len(details_lines) * 35) + 25
        
        min_height = (self.QR_SIZE + 30) if has_qr else 100
        return max(min_height, height)

    def _draw_time_column(self, draw, x, y, h, start_time, end_time):
        draw.rounded_rectangle([x, y, x + 110, y + h], radius=10, fill=self.TIME_BG)
        time_str_start = start_time.strftime('%H:%M')
        time_str_end = end_time.strftime('%H:%M')
        text_y = y + 20
        draw.text((x + 15, text_y), time_str_start, font=self.font_time, fill=self.TEXT_MAIN)
        draw.text((x + 15, text_y + 40), time_str_end, font=self.font_time, fill=self.TEXT_SEC)

    def _generate_qr(self, link):
        qr = qrcode.QRCode(box_size=2, border=1)
        qr.add_data(link)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").resize((self.QR_SIZE, self.QR_SIZE))

    def _draw_event_body(self, img, draw, x, y, width, event):
        cursor_y = y
        bar_color = self.ACCENT_GRAY
        
        if "(підгр. 1)" in event.subject or (event.group and "(підгр. 1)" in event.group):
            bar_color = self.ACCENT_BLUE
        elif "(підгр. 2)" in event.subject or (event.group and "(підгр. 2)" in event.group):
            bar_color = self.ACCENT_ORANGE
            
        card_start_x = x + 130
        # Відступ тексту від лівого краю картки, щоб не налізав на кольорову смужку
        subj_x = card_start_x + 40 
        
        has_link = bool(event.links)
        qr_reserved_space = (self.QR_SIZE + 40) if has_link else 40
        max_text_width = width - 130 - 40 - qr_reserved_space
        
        display_subject = event.subject
        if "(підгр." not in display_subject.lower() and event.group:
             display_subject += f" {event.group}"
        
        subject_lines = self._wrap_text(display_subject, self.font_subject, max_text_width)
        block_height = self._calculate_event_block_height(event, width)
        
        card_x1, card_y1 = card_start_x, cursor_y
        card_x2, card_y2 = x + width - 10, cursor_y + block_height
        
        # Тінь
        draw.rounded_rectangle([card_x1 + 3, card_y1 + 3, card_x2 + 3, card_y2 + 3], radius=12, fill="#00000015")
        # Фон картки
        draw.rounded_rectangle([card_x1, card_y1, card_x2, card_y2], radius=12, fill=self.CARD_BG)
        # Кольоровий маркер
        draw.rounded_rectangle([card_start_x + 8, cursor_y + 12, card_start_x + 18, cursor_y + block_height - 12], radius=5, fill=bar_color)
        
        if has_link:
            try:
                qr_img = self._generate_qr(event.links[0])
                qr_x = card_x2 - self.QR_SIZE - 20
                qr_y = card_y1 + (block_height - self.QR_SIZE) // 2
                img.paste(qr_img, (int(qr_x), int(qr_y)))
            except Exception: pass

        text_cursor_y = cursor_y + self.CARD_PADDING
        for line in subject_lines:
            draw.text((subj_x, text_cursor_y), line, font=self.font_subject, fill=self.TEXT_MAIN)
            text_cursor_y += 45
            
        text_cursor_y += 5
        
        meta_info = []
        if event.event_type: meta_info.append(f"({event.event_type})")
        if event.room: meta_info.append(f"Ауд: {event.room}")
        if event.is_remote: meta_info.append("Online")
        
        details_to_draw = []
        if meta_info: details_to_draw.append(" | ".join(meta_info))
        if event.teacher: details_to_draw.append(f"Викл: {event.teacher}")
        
        for detail in details_to_draw:
            wrapped_detail = self._wrap_text(detail, self.font_details, max_text_width)
            for d_line in wrapped_detail:
                draw.text((subj_x, text_cursor_y), d_line, font=self.font_details, fill=self.TEXT_SEC)
                text_cursor_y += 35
                
        return block_height

    def create_day_image(self, events, date_obj) -> BytesIO:
        events.sort(key=lambda x: x.start_time)
        grouped_events = defaultdict(list)
        for e in events:
            time_key = (e.start_time, e.end_time)
            grouped_events[time_key].append(e)
            
        sorted_keys = sorted(grouped_events.keys())
        total_content_height = 0
        group_heights = {}
        
        for key in sorted_keys:
            group = grouped_events[key]
            group.sort(key=lambda x: x.group if x.group else "") 
            h_acc = 0
            for i, ev in enumerate(group):
                h_acc += self._calculate_event_block_height(ev, self.WIDTH - (self.PADDING*2))
                if i < len(group) - 1: h_acc += 25
            group_heights[key] = h_acc
            total_content_height += h_acc + 40
            
        img_h = max(400, 150 + total_content_height)
        img = Image.new('RGB', (self.WIDTH, img_h), color=self.BG_COLOR)
        draw = ImageDraw.Draw(img)
        
        date_str = date_obj.strftime("%d.%m.%Y")
        weekday = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"][date_obj.weekday()]
        draw.text((self.PADDING, self.PADDING), f"{date_str} ({weekday})", font=self.font_header, fill=self.TEXT_MAIN)
        
        cursor_y = 120
        if not events:
            draw.text((self.PADDING, cursor_y), "Пар немає!", font=self.font_subject, fill=self.TEXT_SEC)
            cursor_y += 60
        else:
            for key in sorted_keys:
                slot_h = group_heights[key]
                self._draw_time_column(draw, self.PADDING, cursor_y, slot_h, key[0], key[1])
                sub_y = cursor_y
                for i, ev in enumerate(grouped_events[key]):
                    h = self._draw_event_body(img, draw, self.PADDING, sub_y, self.WIDTH - (self.PADDING*2), ev)
                    sub_y += h + (25 if i < len(grouped_events[key]) - 1 else 0)
                cursor_y += slot_h + 40

        final_img = img.crop((0, 0, self.WIDTH, cursor_y + 20))
        bio = BytesIO()
        final_img.save(bio, 'PNG')
        bio.seek(0)
        return bio

    def create_week_image(self, events, start_date) -> BytesIO:
        day_images = []
        total_height = 160
        for i in range(7):
            curr_date = start_date + timedelta(days=i)
            day_evs = [e for e in events if e.start_time.date() == curr_date]
            if not day_evs and i >= 5: continue
            bio = self.create_day_image(day_evs, curr_date)
            d_img = Image.open(bio)
            day_images.append(d_img)
            total_height += d_img.height + 20

        final_img = Image.new('RGB', (self.WIDTH, total_height), color=self.BG_COLOR)
        draw = ImageDraw.Draw(final_img)
        end_date = start_date + timedelta(days=6)
        draw.text((self.PADDING, 40), f"Тиждень: {start_date.strftime('%d.%m')} - {end_date.strftime('%d.%m')}", font=self.font_header, fill=self.TEXT_MAIN)
        
        cursor_y = 140
        for d_img in day_images:
            final_img.paste(d_img, (0, cursor_y))
            cursor_y += d_img.height + 20
            
        res = BytesIO()
        final_img.save(res, 'PNG')
        res.seek(0)
        return res

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
        self.TIME_BG = "#E1E8ED"
        
        self.ACCENT_BLUE = "#3498db"
        self.ACCENT_ORANGE = "#e67e22"
        self.ACCENT_GRAY = "#95a5a6"

        self.WIDTH = 1200
        self.PADDING = 40
        self.QR_SIZE = 150
        
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
            avg_char_width = font.getlength("a")
        except AttributeError:
            avg_char_width = font.getsize("a")[0]
            
        if avg_char_width == 0: avg_char_width = 10
        max_chars = int(max_width / avg_char_width)
        for paragraph in text.split('\n'):
            lines.extend(textwrap.wrap(paragraph, width=max_chars))
        return lines

    def _calculate_event_block_height(self, event, width):
        has_qr = bool(event.links)
        
        qr_reserved_space = (self.QR_SIZE + 40) if has_qr else 0
        max_text_width = width - 160 - qr_reserved_space
        
        display_subject = event.subject
        # Логіка додавання групи до назви для розрахунку висоти
        if "(підгр." not in display_subject.lower() and event.group:
             display_subject += f" {event.group}"

        subject_lines = self._wrap_text(display_subject, self.font_subject, max_text_width)
        height = (len(subject_lines) * 45) + 5
        
        meta_info = []
        if event.event_type: meta_info.append(f"({event.event_type})")
        if event.room: meta_info.append(f"Ауд: {event.room}")
        if event.is_remote: meta_info.append("Online")
        if event.teacher: meta_info.append(f"Викл: {event.teacher}")
        
        details_text = " | ".join(meta_info)
        details_lines = self._wrap_text(details_text, self.font_details, max_text_width)
        
        height += (len(details_lines) * 35) + 15
        
        min_height = (self.QR_SIZE + 20) if has_qr else 90
        return max(min_height, height)

    def _draw_time_column(self, draw, x, y, h, start_time, end_time):
        draw.rounded_rectangle([x, y, x + 110, y + h], radius=10, fill=self.TIME_BG)
        time_str_start = start_time.strftime('%H:%M')
        time_str_end = end_time.strftime('%H:%M')
        text_y = y + 15
        draw.text((x + 15, text_y), time_str_start, font=self.font_time, fill=self.TEXT_MAIN)
        draw.text((x + 15, text_y + 35), time_str_end, font=self.font_time, fill=self.TEXT_SEC)

    def _generate_qr(self, link):
        qr = qrcode.QRCode(box_size=2, border=1)
        qr.add_data(link)
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").resize((self.QR_SIZE, self.QR_SIZE))

    def _draw_event_body(self, img, draw, x, y, width, event):
        cursor_y = y
        bar_color = self.ACCENT_GRAY
        
        # --- FIX START: Покращена логіка визначення кольору ---
        # Перевіряємо і назву, і групу, ігноруючи регістр
        check_text = (event.subject + " " + event.group).lower()
        
        if "підгр. 1" in check_text or "підгр.1" in check_text:
            bar_color = self.ACCENT_BLUE
        elif "підгр. 2" in check_text or "підгр.2" in check_text:
            bar_color = self.ACCENT_ORANGE
        # ------------------------------------------------------
            
        subj_x = x + 140
        
        has_link = bool(event.links)
        qr_reserved_space = (self.QR_SIZE + 40) if has_link else 0
        max_text_width = width - 160 - qr_reserved_space
        
        # --- FIX START: Відновлення відображення групи в назві ---
        display_subject = event.subject
        # Якщо в самій назві немає тексту "(підгр.", але в event.group він є - додаємо його
        if "(підгр." not in display_subject.lower() and event.group:
             display_subject += f" {event.group}"
        # ------------------------------------------------------
        
        subject_lines = self._wrap_text(display_subject, self.font_subject, max_text_width)
        
        block_height = self._calculate_event_block_height(event, width)
        
        draw.rectangle([subj_x - 15, cursor_y + 5, subj_x - 8, cursor_y + block_height - 5], fill=bar_color)
        
        if has_link:
            try:
                qr_img = self._generate_qr(event.links[0])
                qr_x = x + width - self.QR_SIZE - 20
                qr_y = cursor_y + 10
                img.paste(qr_img, (int(qr_x), int(qr_y)))
            except Exception as e:
                print(f"QR Error: {e}")

        for line in subject_lines:
            draw.text((subj_x, cursor_y), line, font=self.font_subject, fill=self.TEXT_MAIN)
            cursor_y += 45
            
        cursor_y += 5
        
        meta_info = []
        if event.event_type: meta_info.append(f"({event.event_type})")
        if event.room: meta_info.append(f"Ауд: {event.room}")
        if event.is_remote: meta_info.append("Online")
        
        line1 = " | ".join(meta_info)
        lines_to_draw = []
        if line1: lines_to_draw.append(line1)
        if event.teacher: lines_to_draw.append(f"Викл: {event.teacher}")
        
        for text_part in lines_to_draw:
            wrapped = self._wrap_text(text_part, self.font_details, max_text_width)
            for w_line in wrapped:
                draw.text((subj_x, cursor_y), w_line, font=self.font_details, fill=self.TEXT_SEC)
                cursor_y += 35
                
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
            group.sort(key=lambda x: x.group) 
            h_acc = 0
            for ev in group:
                h_acc += self._calculate_event_block_height(ev, self.WIDTH) + 20
            group_heights[key] = h_acc
            total_content_height += h_acc + 20
            
        final_height = max(400, 150 + total_content_height)
        
        img = Image.new('RGB', (self.WIDTH, final_height), color=self.BG_COLOR)
        draw = ImageDraw.Draw(img)
        
        date_str = date_obj.strftime("%d.%m.%Y")
        weekday = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"][date_obj.weekday()]
        header_text = f"{date_str} ({weekday})"
        draw.text((self.PADDING, self.PADDING), header_text, font=self.font_header, fill=self.TEXT_MAIN)
        
        cursor_y = 120
        
        if not events:
            draw.text((self.PADDING, cursor_y), "Пар немає, можна відпочивати!", font=self.font_subject, fill=self.TEXT_SEC)
        else:
            for key in sorted_keys:
                group = grouped_events[key]
                group.sort(key=lambda x: x.group)
                slot_height = group_heights[key] - 20 
                self._draw_time_column(draw, self.PADDING, cursor_y, slot_height, key[0], key[1])
                sub_cursor_y = cursor_y
                for ev in group:
                    h = self._draw_event_body(img, draw, self.PADDING, sub_cursor_y, self.WIDTH - (self.PADDING*2), ev)
                    sub_cursor_y += h + 20
                cursor_y += slot_height + 40

        final_img = img.crop((0, 0, self.WIDTH, cursor_y + self.PADDING))
        bio = BytesIO()
        final_img.save(bio, 'PNG')
        bio.seek(0)
        return bio

    def create_week_image(self, events, start_date) -> BytesIO:
        day_images = []
        total_height = 160
        for i in range(7):
            current_date = start_date + timedelta(days=i)
            day_events = [e for e in events if e.start_time.date() == current_date]
            if not day_events and i >= 5: continue
            bio = self.create_day_image(day_events, current_date)
            day_img = Image.open(bio)
            day_images.append(day_img)
            total_height += day_img.height + 20

        final_img = Image.new('RGB', (self.WIDTH, total_height), color=self.BG_COLOR)
        draw = ImageDraw.Draw(final_img)
        
        end_date = start_date + timedelta(days=6)
        week_title = f"Тиждень: {start_date.strftime('%d.%m')} - {end_date.strftime('%d.%m')}"
        draw.text((self.PADDING, 40), week_title, font=self.font_header, fill=self.TEXT_MAIN)
        
        cursor_y = 140
        for d_img in day_images:
            final_img.paste(d_img, (0, cursor_y))
            cursor_y += d_img.height + 20
            
        bio = BytesIO()
        final_img.save(bio, 'PNG')
        bio.seek(0)
        return bio

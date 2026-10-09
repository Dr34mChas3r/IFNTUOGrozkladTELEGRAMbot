import textwrap
from datetime import datetime, timedelta
from io import BytesIO
from collections import defaultdict
from PIL import Image, ImageDraw, ImageFont
import qrcode
import re
import pytz

try:
    from pilmoji import Pilmoji
    HAS_PILMOJI = True
except ImportError:
    print("⚠️ УВАГА: Бібліотека pilmoji не знайдена! Емодзі можуть відображатися як квадратики. Встановіть: pip install pilmoji")
    HAS_PILMOJI = False

KYIV_TZ = pytz.timezone('Europe/Kyiv')


class ScheduleImageGenerator:
    def __init__(self, font_path="Roboto-Regular.ttf"):
        self.WIDTH = 1200
        self.PADDING = 40
        self.QR_SIZE = 130
        self.CARD_PADDING = 25
        self.FIXED_CARD_WIDTH = self.WIDTH - 210
        
        try:
            self.font_header  = ImageFont.truetype(font_path, 42)
            self.font_time    = ImageFont.truetype(font_path, 34)
            self.font_subject = ImageFont.truetype(font_path, 36)
            self.font_details = ImageFont.truetype(font_path, 28)
            self.font_status  = ImageFont.truetype(font_path, 24)
            self.font_matrix_subj  = ImageFont.truetype(font_path, 18)
            self.font_matrix_det   = ImageFont.truetype(font_path, 14)
            self.font_matrix_time  = ImageFont.truetype(font_path, 25)
            self.font_matrix_date  = ImageFont.truetype(font_path, 21)
            self.font_matrix_empty = ImageFont.truetype(font_path, 22)
            self.font_matrix_header_day = ImageFont.truetype(font_path, 30)
        except OSError:
            self.font_header  = ImageFont.load_default()
            self.font_time    = ImageFont.load_default()
            self.font_subject = ImageFont.load_default()
            self.font_details = ImageFont.load_default()
            self.font_status  = ImageFont.load_default()
            self.font_matrix_subj  = ImageFont.load_default()
            self.font_matrix_det   = ImageFont.load_default()
            self.font_matrix_time  = ImageFont.load_default()
            self.font_matrix_date  = ImageFont.load_default()
            self.font_matrix_empty = ImageFont.load_default()
            self.font_matrix_header_day = ImageFont.load_default()

    def _get_theme_colors(self, theme: str) -> dict:
        if theme == "dark":
            return {
                'bg': "#18191A",
                'text_main': "#E4E6EB",
                'text_sec': "#B0B3B8",
                'time_bg': "#242526",
                'card_bg': "#242526",
                'border': "#3A3B3C",
                'unsel_bg': "#242526",
                'unsel_text': "#65676B",
                'unsel_bar': "#3A3B3C",
                'accent_blue': "#3B82F6",
                'accent_orange': "#F59E0B",
                'accent_green': "#10B981",
                'accent_red': "#EF4444",
                'shadow': "#00000040",
                'empty_bg': "#242526",
                'badge_text': "#FFFFFF"
            }
        else:
            return {
                'bg': "#F0F2F5",
                'text_main': "#000000",
                'text_sec': "#555555",
                'time_bg': "#E1E8ED",
                'card_bg': "#FFFFFF",
                'border': "#D0D7DE",
                'unsel_bg': "#F7F9FA",
                'unsel_text': "#9AA0A6",
                'unsel_bar': "#D0D7DE",
                'accent_blue': "#0000ff",
                'accent_orange': "#ffa500",
                'accent_green': "#008000",
                'accent_red': "#d60000",
                'shadow': "#00000008",
                'empty_bg': "#F0F4F8",
                'badge_text': "#FFFFFF"
            }

    def _draw_text(self, draw, pilmoji, pos, text, font, fill, align="left"):
        if pilmoji:
            pilmoji.text(pos, text, font=font, fill=fill, align=align)
        else:
            if "\n" in text:
                draw.multiline_text(pos, text, font=font, fill=fill, align=align)
            else:
                draw.text(pos, text, font=font, fill=fill, align=align)

    def _to_local_dt(self, dt):
        """Переводить datetime у київський час з урахуванням літнього/зимового (DST)."""
        if not isinstance(dt, datetime):
            return dt
        if dt.tzinfo is None:
            return KYIV_TZ.localize(dt)
        return dt.astimezone(KYIV_TZ)

    def _to_local_date(self, dt):
        if isinstance(dt, datetime):
            return self._to_local_dt(dt).date()
        return dt

    def _sort_events_globally(self, events):
        def get_sort_key(ev):
            text = (ev.subject + str(ev.group or "")).lower()
            sg_order = 3
            if "підгр. 1" in text: 
                sg_order = 1
            elif "підгр. 2" in text: 
                sg_order = 2
            return (ev.start_time, sg_order)
            
        return sorted(events, key=get_sort_key)

    def _get_w(self, font, text):
        if hasattr(font, 'getlength'):
            return font.getlength(text)
        return font.getsize(text)[0]

    def _wrap_text(self, text, font, max_px_width):
        if not text:
            return []
        lines = []
        for paragraph in text.split('\n'):
            words = paragraph.split()
            current_line = []
            for word in words:
                current_line.append(word)
                if self._get_w(font, " ".join(current_line)) > max_px_width:
                    if len(current_line) == 1:
                        lines.append(current_line[0])
                        current_line = []
                    else:
                        current_line.pop()
                        lines.append(" ".join(current_line))
                        current_line = [word]
            if current_line:
                lines.append(" ".join(current_line))
        return lines

    def _prepare_event_content(self, event):
        is_cancelled = getattr(event, 'is_cancelled', False)
        is_unselected = getattr(event, 'is_unselected', False)
        is_elective = getattr(event, 'is_elective', False)
        
        has_qr = bool(event.links) and not is_cancelled and not is_unselected
        qr_space = (self.QR_SIZE + 30) if has_qr else 0
        content_max_w = self.FIXED_CARD_WIDTH - qr_space - (self.CARD_PADDING * 2) - 50

        subj_raw = event.subject
        grp_raw  = event.group if event.group else ""
        has_sg1  = "підгр. 1" in subj_raw.lower() or "підгр. 1" in grp_raw.lower()
        has_sg2  = "підгр. 2" in subj_raw.lower() or "підгр. 2" in grp_raw.lower()
        
        has_badge = is_cancelled or has_sg1 or has_sg2 or is_elective
        badge_h  = 45 if has_badge else 0

        display_subj = event.subject
        if event.group and "(підгр." not in display_subj.lower():
            display_subj += f" {event.group}"

        subj_lines   = self._wrap_text(display_subj, self.font_subject, content_max_w)
        subj_h       = len(subj_lines) * 48

        meta = []
        if event.event_type: meta.append(f"({event.event_type})")
        if event.room:       meta.append(f"Ауд: {event.room}")
        if event.is_remote:  meta.append("Online")
        meta_lines = self._wrap_text(" | ".join(meta), self.font_details, content_max_w)
        meta_h     = len(meta_lines) * 38

        teacher_lines = self._wrap_text(f"Викл: {event.teacher}" if event.teacher else "", self.font_details, content_max_w)
        teacher_h     = len(teacher_lines) * 38

        total_h = self.CARD_PADDING * 2 + badge_h + subj_h + meta_h + teacher_h + 15
        min_h   = (self.QR_SIZE + self.CARD_PADDING * 2) if has_qr else 120

        return {
            'lines':       {'subj': subj_lines, 'meta': meta_lines, 'teacher': teacher_lines},
            'height':      max(min_h, total_h),
            'has_qr':      has_qr,
            'is_cancelled':is_cancelled,
            'is_unselected':is_unselected,
            'is_elective': is_elective,
            'has_sg1':     has_sg1,
            'has_sg2':     has_sg2,
            'subj_lines':  subj_lines,
        }

    def _draw_event_card(self, img, draw, pilmoji, x, y, event, colors):
        data   = self._prepare_event_content(event)
        card_x1 = x + 130
        card_x2 = card_x1 + self.FIXED_CARD_WIDTH
        card_h  = data['height']
        
        is_unsel = data['is_unselected']
        
        bg_color = colors['unsel_bg'] if is_unsel else colors['card_bg']
        txt_main = colors['unsel_text'] if is_unsel else colors['text_main']
        txt_sec  = colors['unsel_text'] if is_unsel else colors['text_sec']

        bar_color = colors['accent_green']
        if data['has_sg1']:   bar_color = colors['accent_blue']
        elif data['has_sg2']: bar_color = colors['accent_orange']
        if data['is_cancelled'] and not (data['has_sg1'] or data['has_sg2']):
            bar_color = colors['accent_red']
            
        if is_unsel:
            bar_color = colors['unsel_bar']

        draw.rounded_rectangle([card_x1+3, y+3, card_x2+3, y+card_h+3], radius=15, fill=colors['shadow'])
        draw.rounded_rectangle([card_x1,   y,   card_x2,   y+card_h],   radius=15, fill=bg_color)
        draw.rounded_rectangle([card_x1+10, y+15, card_x1+18, y+card_h-15], radius=4, fill=bar_color)

        curr_y  = y + self.CARD_PADDING
        badge_x = card_x1 + 35
        badge_added = False

        if data['is_elective']:
            badge_bg = colors['accent_red'] if is_unsel else colors['accent_green']
            badge_text = "НЕ ОБРАНО" if is_unsel else "ОБРАНО"
            
            draw.rounded_rectangle([badge_x, curr_y, badge_x+165, curr_y+35], radius=8, fill=badge_bg)
            self._draw_text(draw, pilmoji, (badge_x+15, curr_y+4), badge_text, font=self.font_status, fill=colors['badge_text'])
            badge_x += 180
            badge_added = True

        if data['has_sg1']:
            badge_bg = colors['unsel_bar'] if is_unsel else colors['accent_blue']
            draw.rounded_rectangle([badge_x, curr_y, badge_x+165, curr_y+35], radius=8, fill=badge_bg)
            self._draw_text(draw, pilmoji, (badge_x+15, curr_y+4), "Підгрупа 1", font=self.font_status, fill=colors['badge_text'])
            badge_x += 180; badge_added = True
        elif data['has_sg2']:
            badge_bg = colors['unsel_bar'] if is_unsel else colors['accent_orange']
            draw.rounded_rectangle([badge_x, curr_y, badge_x+165, curr_y+35], radius=8, fill=badge_bg)
            self._draw_text(draw, pilmoji, (badge_x+15, curr_y+4), "Підгрупа 2", font=self.font_status, fill=colors['badge_text'])
            badge_x += 180; badge_added = True

        if data['is_cancelled']:
            draw.rounded_rectangle([badge_x, curr_y, badge_x+165, curr_y+35], radius=8, fill=colors['unsel_bar'] if is_unsel else colors['accent_red'])
            self._draw_text(draw, pilmoji, (badge_x+15, curr_y+4), "ВІДМІНЕНО", font=self.font_status, fill=colors['badge_text'])
            badge_added = True

        if badge_added: curr_y += 45

        if data['has_qr']:
            try:
                qr = qrcode.QRCode(box_size=2, border=1)
                qr.add_data(event.links[0]); qr.make(fit=True)
                
                qr_img = qr.make_image(fill_color="black", back_color="white").resize((self.QR_SIZE, self.QR_SIZE))
                img.paste(qr_img, (int(card_x2 - self.QR_SIZE - 20), int(y + self.CARD_PADDING)))
            except: pass

        subj_x = card_x1 + 35
        for line in data['subj_lines']:
            self._draw_text(draw, pilmoji, (subj_x, curr_y), line, font=self.font_subject, fill=txt_main)
            curr_y += 48

        for line in data['lines']['meta']:
            self._draw_text(draw, pilmoji, (subj_x, curr_y+5), line, font=self.font_details, fill=txt_sec)
            curr_y += 38

        for line in data['lines']['teacher']:
            self._draw_text(draw, pilmoji, (subj_x, curr_y+5), line, font=self.font_details, fill=txt_sec)

        return card_h

    def _draw_time_column(self, draw, pilmoji, x, y, h, start_time, end_time, colors):
        draw.rounded_rectangle([x, y, x+110, y+h], radius=15, fill=colors['time_bg'])
        self._draw_text(draw, pilmoji, (x+15, y+20), start_time.strftime('%H:%M'), font=self.font_time, fill=colors['text_main'])
        self._draw_text(draw, pilmoji, (x+15, y+60), end_time.strftime('%H:%M'),   font=self.font_time, fill=colors['text_sec'])

    def create_day_image(self, events, date_obj, theme="light") -> BytesIO:
        colors = self._get_theme_colors(theme)
        events = self._sort_events_globally(events)
        
        grouped = defaultdict(list)
        for e in events:
            grouped[(e.start_time, e.end_time)].append(e)

        sorted_keys = sorted(grouped.keys())
        total_h  = 150
        slot_data = {}

        for key in sorted_keys:
            group = grouped[key]
            h_acc = 0
            for i, ev in enumerate(group):
                prep   = self._prepare_event_content(ev)
                h_acc += prep['height']
                if i < len(group)-1: h_acc += 20
            slot_data[key] = h_acc
            total_h += h_acc + 40

        img  = Image.new('RGB', (self.WIDTH, max(400, total_h)), color=colors['bg'])
        draw = ImageDraw.Draw(img)
        pilmoji = Pilmoji(img) if HAS_PILMOJI else None

        local_date = self._to_local_date(date_obj)
        day_names = ['Понеділок','Вівторок','Середа','Четвер',"П'ятниця","Субота","Неділя"]
        header = f"{local_date.strftime('%d.%m.%Y')} ({day_names[local_date.weekday()]})"
        
        self._draw_text(draw, pilmoji, (self.PADDING, self.PADDING), header, font=self.font_header, fill=colors['text_main'])

        cursor_y = 130
        if not events:
            self._draw_text(draw, pilmoji, (self.PADDING, cursor_y), "Пар немає, можна відпочивати! 🏖️", font=self.font_subject, fill=colors['text_sec'])
            cursor_y += 80 
        else:
            for key in sorted_keys:
                h = slot_data[key]
                self._draw_time_column(draw, pilmoji, self.PADDING, cursor_y, h, key[0], key[1], colors)
                sub_y = cursor_y
                for i, ev in enumerate(grouped[key]):
                    sub_y += self._draw_event_card(img, draw, pilmoji, self.PADDING, sub_y, ev, colors)
                    if i < len(grouped[key])-1: sub_y += 20
                cursor_y += h + 40

        if pilmoji: pilmoji.close()

        bio = BytesIO()
        img.crop((0, 0, self.WIDTH, cursor_y + 20)).save(bio, 'PNG')
        bio.seek(0)
        return bio

    def _draw_matrix_card(self, draw, pilmoji, x, y, w, h, events_in_cell, colors):
        def get_sg_order(ev):
            text = (ev.subject + str(ev.group or "")).lower()
            if "підгр. 1" in text: return 1
            if "підгр. 2" in text: return 2
            return 3
            
        events_in_cell = sorted(events_in_cell, key=get_sg_order)

        has_sg1 = has_sg2 = has_can = False
        display_events = []

        for ev in events_in_cell:
            subj_raw   = ev.subject
            grp_raw    = ev.group if ev.group else ""
            ev_has_sg1 = "підгр. 1" in subj_raw.lower() or "підгр. 1" in grp_raw.lower()
            ev_has_sg2 = "підгр. 2" in subj_raw.lower() or "підгр. 2" in grp_raw.lower()
            is_cancelled = getattr(ev, 'is_cancelled', False)
            has_sg1 = has_sg1 or ev_has_sg1
            has_sg2 = has_sg2 or ev_has_sg2
            has_can = has_can or is_cancelled
            display_events.append((ev, is_cancelled, ev_has_sg1, ev_has_sg2))

        is_all_unsel = all(getattr(ev, 'is_unselected', False) for ev in events_in_cell)

        bar_color = colors['accent_green']
        if has_sg1 and not has_sg2:   bar_color = colors['accent_blue']
        elif has_sg2 and not has_sg1: bar_color = colors['accent_orange']
        elif has_can:                 bar_color = colors['accent_red']
        
        if is_all_unsel:
            bar_color = colors['unsel_bar']

        bg_color = colors['unsel_bg'] if (has_can or is_all_unsel) else colors['card_bg']

        draw.rounded_rectangle([x, y, x+w, y+h], radius=4,
                                fill=bg_color,
                                outline=colors['border'], width=1)

        if has_sg1 and has_sg2 and not is_all_unsel:
            mid = y + h // 2
            draw.rounded_rectangle([x+4, y+4,  x+12, mid],    radius=4, fill=colors['accent_blue'])
            draw.rounded_rectangle([x+4, mid,   x+12, y+h-4], radius=4, fill=colors['accent_orange'])
        else:
            draw.rounded_rectangle([x+4, y+4, x+12, y+h-4], radius=4, fill=bar_color)

        txt_x = x + 18
        txt_w = w - 24

        num_ev  = len(display_events)
        curr_y  = y + 8

        for ev, is_can, es1, es2 in display_events:
            is_unsel = getattr(ev, 'is_unselected', False)
            txt_main = colors['unsel_text'] if is_unsel else colors['text_main']
            txt_sec  = colors['unsel_text'] if is_unsel else colors['text_sec']
            
            display_subj = ev.subject
            if is_unsel:
                display_subj = "❌ " + display_subj
            elif is_can:
                display_subj = "[ВІДМІНА] " + display_subj \
                    .replace("[Увага! ЗАНЯТТЯ ВІДМІНЕНО!]", "") \
                    .replace("ВІДМІНЕНО!", "").strip()

            if es1: display_subj += " (підгр. 1)"
            if es2: display_subj += " (підгр. 2)"

            subj_lines = self._wrap_text(display_subj, self.font_matrix_subj, txt_w)

            available_h  = (y + h - 8) - curr_y
            max_subj_lines = max(1, (available_h - 36) // 22)
            if num_ev > 1:
                max_subj_lines = min(max_subj_lines, 3)

            for line in subj_lines[:max_subj_lines]:
                if curr_y + 20 > y + h - 6: break
                self._draw_text(draw, pilmoji, (txt_x, curr_y), line, font=self.font_matrix_subj, fill=txt_main)
                curr_y += 22

            meta = []
            if ev.event_type: meta.append(ev.event_type)
            if ev.room:        meta.append(ev.room)
            meta_str = " | ".join(meta)
            if meta_str:
                for ml in self._wrap_text(meta_str, self.font_matrix_det, txt_w)[:2]:
                    if curr_y + 16 > y + h - 6: break
                    self._draw_text(draw, pilmoji, (txt_x, curr_y + 2), ml, font=self.font_matrix_det, fill=txt_sec)
                    curr_y += 18

            if ev.teacher:
                for tl in self._wrap_text(ev.teacher, self.font_matrix_det, txt_w)[:2]:
                    if curr_y + 16 > y + h - 6: break
                    self._draw_text(draw, pilmoji, (txt_x, curr_y + 2), tl, font=self.font_matrix_det, fill=txt_sec)
                    curr_y += 18

            curr_y += 6

    def _estimate_cell_height(self, events_in_cell, txt_w):
        total = 8
        for ev in events_in_cell:
            subj_raw = ev.subject
            grp_raw  = ev.group if ev.group else ""
            es1 = "підгр. 1" in subj_raw.lower() or "підгр. 1" in grp_raw.lower()
            es2 = "підгр. 2" in subj_raw.lower() or "підгр. 2" in grp_raw.lower()
            is_unsel = getattr(ev, 'is_unselected', False)
            
            display_subj = ev.subject
            if is_unsel: display_subj = "❌ " + display_subj
            if es1: display_subj += " (підгр. 1)"
            if es2: display_subj += " (підгр. 2)"
            
            subj_lines = self._wrap_text(display_subj, self.font_matrix_subj, txt_w)
            total += len(subj_lines) * 22

            meta = []
            if ev.event_type: meta.append(ev.event_type)
            if ev.room:        meta.append(ev.room)
            meta_str = " | ".join(meta)
            if meta_str:
                total += len(self._wrap_text(meta_str, self.font_matrix_det, txt_w)[:2]) * 18
            if ev.teacher:
                total += len(self._wrap_text(ev.teacher, self.font_matrix_det, txt_w)[:2]) * 18
            total += 6
        return total + 12

    def create_week_image(self, events, start_date, theme="light") -> BytesIO:
        colors = self._get_theme_colors(theme)
        events = self._sort_events_globally(events)

        local_start = self._to_local_date(start_date)
        monday = local_start - timedelta(days=local_start.weekday())

        has_saturday = any(
            (self._to_local_date(e.start_time) - monday).days == 5
            for e in events
        )
        cols = 6 if has_saturday else 5

        ROW_H_MIN = 160
        col_w    = 260
        time_w   = 120
        header_h = 100
        padding  = 30

        width = padding * 2 + time_w + cols * col_w
        txt_w  = col_w - 24

        PAIR_TIMES = [
            ("08:00", "09:20"), ("09:30", "10:50"), ("11:00", "12:20"),
            ("12:50", "14:10"), ("14:20", "15:40"), ("15:50", "17:10"),
            ("17:20", "18:40"), ("18:50", "20:10"),
        ]

        grid           = {d: {p: [] for p in range(len(PAIR_TIMES))} for d in range(cols)}
        active_pairs   = set()
        day_has_events = {d: False for d in range(cols)}

        for ev in events:
            local_dt = self._to_local_dt(ev.start_time)
            d_idx = (local_dt.date() - monday).days
            if 0 <= d_idx < cols:
                time_str = local_dt.strftime("%H:%M")

                for i, (start, _) in enumerate(PAIR_TIMES):
                    if time_str == start:
                        grid[d_idx][i].append(ev)
                        break

        vp_days = {}
        for d in range(cols):
            day_events = [ev for p in range(len(PAIR_TIMES)) for ev in grid[d][p]]
            if day_events and all("військова підготовка" in ev.raw_subject.lower() for ev in day_events):
                vp_days[d] = day_events
                for p in range(len(PAIR_TIMES)):
                    grid[d][p] = []

        for d in range(cols):
            if d in vp_days:
                day_has_events[d] = True
            for p_idx in range(len(PAIR_TIMES)):
                if grid[d][p_idx]:
                    active_pairs.add(p_idx)
                    day_has_events[d] = True

        if not active_pairs:
            min_pair, max_pair = 0, 3
        else:
            min_pair, max_pair = min(active_pairs), max(active_pairs)

        row_heights = {}
        for p_idx in range(min_pair, max_pair + 1):
            max_needed = ROW_H_MIN
            for d in range(cols):
                cell = grid[d][p_idx]
                if cell:
                    needed = self._estimate_cell_height(cell, txt_w)
                    max_needed = max(max_needed, needed)
            row_heights[p_idx] = max_needed

        total_rows_h = sum(row_heights.values())
        if total_rows_h == 0: total_rows_h = ROW_H_MIN * 4
        height = padding * 2 + header_h + total_rows_h

        img  = Image.new('RGB', (width, height), color=colors['bg'])
        draw = ImageDraw.Draw(img)
        pilmoji = Pilmoji(img) if HAS_PILMOJI else None

        full_day_names = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"]

        for d in range(cols):
            curr_date = monday + timedelta(days=d)
            x = padding + time_w + d * col_w
            draw.rounded_rectangle([x+2, padding, x+col_w-2, padding+header_h-10],
                                    radius=4, fill=colors['card_bg'], outline=colors['border'], width=1)

            day_text  = full_day_names[d]
            date_text = f"({curr_date.strftime('%d.%m')})"

            bbox_d  = draw.textbbox((0,0), day_text,  font=self.font_matrix_header_day)
            self._draw_text(draw, pilmoji, (x + (col_w-(bbox_d[2]-bbox_d[0]))/2, padding+15),
                       day_text, font=self.font_matrix_header_day, fill=colors['text_main'])

            bbox_da = draw.textbbox((0,0), date_text, font=self.font_matrix_date)
            self._draw_text(draw, pilmoji, (x + (col_w-(bbox_da[2]-bbox_da[0]))/2, padding+55),
                       date_text, font=self.font_matrix_date, fill=colors['text_sec'])

        curr_y = padding + header_h

        for row_idx, p_idx in enumerate(range(min_pair, max_pair + 1)):
            start_t, end_t = PAIR_TIMES[p_idx]
            row_h = row_heights[p_idx]

            tx = padding
            tw = time_w - 4
            th = row_h - 6
            draw.rounded_rectangle([tx, curr_y, tx+tw, curr_y+th],
                                    radius=4, fill=colors['time_bg'],
                                    outline=colors['border'], width=1)

            bb_s = draw.textbbox((0,0), start_t, font=self.font_matrix_time)
            self._draw_text(draw, pilmoji, (tx + (tw-(bb_s[2]-bb_s[0]))/2, curr_y + 38),
                       start_t, font=self.font_matrix_time, fill=colors['text_main'])

            bb_e = draw.textbbox((0,0), end_t, font=self.font_matrix_date)
            self._draw_text(draw, pilmoji, (tx + (tw-(bb_e[2]-bb_e[0]))/2, curr_y + 76),
                       end_t, font=self.font_matrix_date, fill=colors['text_sec'])

            for d in range(cols):
                if d in vp_days:
                    continue 
                
                x = padding + time_w + d * col_w

                if not day_has_events[d]:
                    if row_idx == 0:
                        empty_h = total_rows_h - 6
                        draw.rounded_rectangle([x+2, curr_y, x+col_w-2, curr_y+empty_h],
                                                radius=4, fill=colors['empty_bg'],
                                                outline=colors['border'], width=1)
                        msg  = "Пар немає!\nМожна відпочивати. 🏖️"
                        bbox = draw.multiline_textbbox((0,0), msg, font=self.font_matrix_empty)
                        tw_m = bbox[2]-bbox[0]; th_m = bbox[3]-bbox[1]
                        self._draw_text(draw, pilmoji, (x+(col_w-tw_m)/2, curr_y+(empty_h-th_m)/2),
                            msg, font=self.font_matrix_empty, fill=colors['text_sec'], align="center")
                    continue

                cell_events = grid[d][p_idx]
                if cell_events:
                    self._draw_matrix_card(draw, pilmoji, x+2, curr_y, col_w-4, row_h-6, cell_events, colors)
                else:
                    draw.rounded_rectangle([x+2, curr_y, x+col_w-2, curr_y+row_h-6],
                                            radius=4, fill=colors['card_bg'],
                                            outline=colors['border'], width=1)

            curr_y += row_h

        for d, vp_events in vp_days.items():
            x = padding + time_w + d * col_w
            y_start = padding + header_h
            h_block = total_rows_h - 6
            
            is_unsel = all(getattr(e, 'is_unselected', False) for e in vp_events)
            
            bg = colors['unsel_bg'] if is_unsel else colors['card_bg']
            bar = colors['unsel_bar'] if is_unsel else colors['accent_green']
            txt_color = colors['unsel_text'] if is_unsel else colors['text_main']
            
            draw.rounded_rectangle([x+2, y_start, x+col_w-2, y_start+h_block], radius=4, fill=bg, outline=colors['border'], width=1)
            draw.rounded_rectangle([x+4, y_start+4, x+12, y_start+h_block-4], radius=4, fill=bar)
            
            msg = "Військова\nпідготовка"
            if is_unsel:
                msg = "❌ НЕ ОБРАНО\n" + msg
                
            vp_font = self.font_details
                
            bbox = draw.multiline_textbbox((0,0), msg, font=vp_font)
            tw_m = bbox[2]-bbox[0]; th_m = bbox[3]-bbox[1]
            
            self._draw_text(draw, pilmoji, (x + 18 + (col_w-24-tw_m)/2, y_start + (h_block-th_m)/2), msg, font=vp_font, fill=txt_color, align="center")

        if pilmoji: pilmoji.close()

        bio = BytesIO()
        img.save(bio, 'PNG')
        bio.seek(0)
        return bio

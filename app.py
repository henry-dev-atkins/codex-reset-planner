#!/usr/bin/env python3
"""Codex Reset Planner — 100% offline Tkinter desktop UI, Python 3.10+."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import calendar
from pathlib import Path
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

from core import (Settings, codex_folder_default, demo_usage, local_time,
                  optimize, scan_codex, UTC)

APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = Path.home() / '.codex-reset-planner' / 'settings.json'
C = {
    'bg': '#0C1221', 'card': '#151E32', 'card2': '#1B2840',
    'line': '#2A3955', 'text': '#F2F6FF', 'muted': '#9EACC7',
    'faint': '#7284A7', 'teal': '#6BE7D5', 'teal_dark': '#17423F',
    'blue': '#93B1FF', 'blue_dark': '#283A65', 'amber': '#F5BC74',
    'amber_dark': '#43321F', 'pink': '#F78FAA', 'pink_dark': '#4C293D',
    'purple': '#BEA0FA', 'purple_dark': '#332852', 'green': '#B2EDA8'
}
FONT = 'DejaVu Sans'


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class ResetApp:
    def __init__(self):
        self.settings = Settings.load(SETTINGS_PATH)
        self.root = tk.Tk()
        self.root.title('Codex Reset Planner')
        self.root.geometry('1280x875')
        self.root.minsize(995, 700)
        self.root.configure(bg=C['bg'])
        try:
            icon = tk.PhotoImage(file=str(APP_DIR / 'assets' / 'icon.png'))
            self.root.iconphoto(True, icon)
            self.icon = icon
        except tk.TclError:
            pass
        self.root.option_add('*Font', (FONT, 11))
        self.scroll = tk.Scrollbar(self.root, orient='vertical', bg=C['card'], troughcolor=C['bg'])
        self.scroll.pack(side='right', fill='y')
        self.canvas = tk.Canvas(self.root, bg=C['bg'], highlightthickness=0,
                                yscrollcommand=self.scroll.set, bd=0)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.scroll.config(command=self.canvas.yview)
        self.hotspots = []
        self.mode = 'steady'
        self.view = 'calendar'
        self.month_offset = 0
        self.now = datetime.now(UTC)
        self.folder = self.settings.folder or str(codex_folder_default())
        self.usage = demo_usage(self.now) if self.settings.demo else scan_codex(self.folder, self.now)
        self.plan = None
        self.plan_other = None
        self.calc_plans()
        self.canvas.bind('<Button-1>', self.on_click)
        self.canvas.bind('<Motion>', self.on_move)
        self.canvas.bind('<MouseWheel>', self.on_wheel)
        self.canvas.bind('<Button-4>', lambda e: self.canvas.yview_scroll(-3, 'units'))
        self.canvas.bind('<Button-5>', lambda e: self.canvas.yview_scroll(3, 'units'))
        self.canvas.bind('<Configure>', self.on_resize)
        self.root.bind('<Control-r>', lambda e: self.refresh())
        self.root.bind('<Command-r>', lambda e: self.refresh())
        self.root.after(100, self.draw)

    def save(self):
        self.settings.save(SETTINGS_PATH)

    def calc_plans(self):
        self.now = datetime.now(UTC)
        obs = self.usage.latest
        daily = self.settings.daily_override
        if daily is None:
            daily = self.usage.daily_rate if self.usage.daily_rate is not None else 12.
        ratio = self.settings.ratio_override
        if ratio is None:
            ratio = self.usage.ratio if self.usage.ratio is not None else 4.
        self.daily = daily
        self.ratio = ratio
        expiry = None
        if self.settings.expiry:
            try:
                expiry = date.fromisoformat(self.settings.expiry)
            except ValueError:
                pass
        self.expiry = expiry
        self.plan = optimize(obs, self.now, daily, ratio, self.settings.banked, self.mode, expiry)
        self.plan_other = optimize(obs, self.now, daily, ratio, self.settings.banked,
                                   'spiky' if self.mode == 'steady' else 'steady', expiry)

    def refresh(self):
        self.usage = demo_usage(datetime.now(UTC)) if self.settings.demo else scan_codex(self.folder)
        self.calc_plans()
        self.draw()

    def update(self):
        self.save()
        self.calc_plans()
        self.draw()

    def change_folder(self):
        selected = filedialog.askdirectory(title='Select your Codex data folder', initialdir=self.folder)
        if selected:
            self.folder = selected
            self.settings.folder = selected
            self.settings.demo = False
            self.save()
            self.refresh()

    def toggle_demo(self):
        self.settings.demo = not self.settings.demo
        self.save()
        self.refresh()

    def adjust_banked(self, delta):
        self.settings.banked = clamp(self.settings.banked + delta, 0, 9)
        self.update()

    def set_expiry(self):
        value = simpledialog.askstring('Saved reset expiry',
            'Enter the expiry date shown in your account (YYYY-MM-DD).\nLeave blank if you do not know it.',
            initialvalue=self.settings.expiry, parent=self.root)
        if value is None:
            return
        value = value.strip()
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                messagebox.showerror('Invalid date', 'Please use YYYY-MM-DD, for example 2026-11-10.')
                return
        self.settings.expiry = value
        self.update()

    def set_daily(self):
        result = simpledialog.askstring('Estimated daily usage',
            'Daily workload in WEEKLY allowance percentage points.\n'
            'For example, 12 means about 12% of the weekly allowance per typical day.\n'
            'Leave empty to use the estimate from logs.',
            initialvalue='' if self.settings.daily_override is None else str(self.settings.daily_override),
            parent=self.root)
        if result is None:
            return
        try:
            value = None if not result.strip() else float(result)
            if value is not None and not 0 <= value <= 100:
                raise ValueError
        except ValueError:
            messagebox.showerror('Invalid value', 'Enter a number from 0 to 100, or leave blank.')
            return
        self.settings.daily_override = value
        self.update()

    def set_ratio(self):
        result = simpledialog.askstring('5-hour / weekly conversion',
            'How many 5-hour usage percentage points correspond to 1 weekly point?\n'
            'This is learned when both rate-limit counters increase in the same window.\n'
            'Leave empty to use observed changes or a rough fallback of 4.0.',
            initialvalue='' if self.settings.ratio_override is None else str(self.settings.ratio_override),
            parent=self.root)
        if result is None:
            return
        try:
            value = None if not result.strip() else float(result)
            if value is not None and not .15 <= value <= 30:
                raise ValueError
        except ValueError:
            messagebox.showerror('Invalid value', 'Enter a number between 0.15 and 30, or leave blank.')
            return
        self.settings.ratio_override = value
        self.update()

    def switch_mode(self, mode):
        self.mode = mode
        self.calc_plans()
        self.draw()

    def switch_view(self, view):
        self.view = view
        self.draw()

    def move_month(self, amount):
        self.month_offset = clamp(self.month_offset + amount, -1, 3)
        self.draw()

    def on_resize(self, event):
        if event.width > 200:
            self.draw()

    def on_click(self, e):
        x, y = e.x, self.canvas.canvasy(e.y)
        for x0, y0, x1, y1, callback, _ in reversed(self.hotspots):
            if x0 <= x <= x1 and y0 <= y <= y1:
                callback()
                return

    def on_move(self, e):
        x, y = e.x, self.canvas.canvasy(e.y)
        found = next((label for x0,y0,x1,y1,_,label in reversed(self.hotspots)
                     if x0 <= x <= x1 and y0 <= y <= y1), None)
        self.canvas.config(cursor='hand2' if found else 'arrow')

    def on_wheel(self, e):
        if sys.platform == 'darwin':
            delta = -int(e.delta)
        else:
            delta = -int(e.delta / 120) * 3
        self.canvas.yview_scroll(delta, 'units')

    def hot(self, x, y, w, h, cb, label='Action'):
        self.hotspots.append((x, y, x+w, y+h, cb, label))

    def rect(self, x, y, w, h, fill, radius=0, outline=None, width=1):
        c = self.canvas
        if radius <= 0:
            return c.create_rectangle(x, y, x+w, y+h, fill=fill,
                 outline=outline if outline else fill, width=width)
        r = min(radius, w/2, h/2)
        points = (x+r,y, x+w-r,y, x+w,y, x+w,y+r,
                  x+w,y+h-r, x+w,y+h, x+w-r,y+h,
                  x+r,y+h, x,y+h, x,y+h-r, x,y+r, x,y)
        return c.create_polygon(points, smooth=True, splinesteps=12,
                     fill=fill, outline=outline or fill, width=width)

    def text(self, x, y, s, size=12, color=None, weight='normal', anchor='nw', width=None):
        opts = dict(text=str(s), fill=color or C['text'], font=(FONT, size, weight),
                    anchor=anchor, justify='left')
        if width:
            opts['width'] = width
        return self.canvas.create_text(x, y, **opts)

    def pill(self, x, y, w, h, label, active=False, cb=None, color=None, small=False):
        fill = C['teal_dark'] if active else C['card2']
        fg = color or (C['teal'] if active else C['muted'])
        self.rect(x, y, w, h, fill, h/2, outline=C['teal_dark'] if active else C['line'])
        self.text(x+w/2, y+h/2, label, 10 if small else 11, fg, 'bold', 'center')
        if cb: self.hot(x, y, w, h, cb, label)

    def small_button(self, x, y, w, label, cb, accent=False):
        self.rect(x, y, w, 32, C['teal_dark'] if accent else C['card2'], 9,
                  C['teal_dark'] if accent else C['line'])
        self.text(x+w/2, y+16, label, 10, C['teal'] if accent else C['text'], 'bold', 'center')
        self.hot(x, y, w, 32, cb, label)

    def section(self, x, y, w, h, bg=None):
        self.rect(x, y, w, h, bg or C['card'], 16, C['line'])

    def bar(self, x, y, w, v, color, bg=None, h=8):
        self.rect(x, y, w, h, bg or C['card2'], h/2)
        if v and v > 0:
            self.rect(x, y, max(3, w * clamp(v, 0, 100) / 100), h, color, h/2)

    def line(self, x0, y0, x1, y1, color=None, width=1):
        self.canvas.create_line(x0,y0,x1,y1,fill=color or C['line'], width=width)

    def last_observation(self):
        o = self.usage.latest
        return o.five_used if o else None, o.week_used if o else None

    def draw(self):
        c = self.canvas
        old_top = c.yview()[0]
        c.delete('all')
        self.hotspots = []
        w = max(c.winfo_width(), 990)
        inset = 27
        leftw = 319
        gap = 20
        rightx = inset + leftw + gap
        rightw = w - rightx - inset
        self.header(w, inset)
        self.left_column(inset, leftw)
        y_end = self.right_column(rightx, rightw)
        footer = max(1010, y_end + 24)
        self.line(27, footer-40, w-27, footer-40)
        self.text(29, footer-27, 'CODEX RESET PLANNER  /  100% LOCAL  /  NO API OR CLI REQUESTS', 9, C['faint'], 'bold')
        self.text(w-29, footer-27, 'Planning estimates · not account actions', 9, C['faint'], anchor='ne')
        c.configure(scrollregion=(0,0,w,footer+18))
        c.yview_moveto(old_top)

    def header(self, w, x):
        # Small geometric mark and reserved offline aesthetic.
        self.rect(x, 27, 41, 41, C['teal_dark'], 13)
        self.canvas.create_arc(x+9, 35, x+32, 59, start=40, extent=270,
                 style='arc', outline=C['teal'], width=3)
        self.canvas.create_oval(x+28, 38, x+33, 43, fill=C['teal'], outline=C['teal'])
        self.text(x+54, 28, 'codex / reset planner', 17, C['text'], 'bold')
        self.text(x+55, 53, 'PRIVATE DESKTOP UTILITY', 9, C['faint'], 'bold')
        self.small_button(w-258, 32, 132, 'Change folder', self.change_folder)
        self.small_button(w-112, 32, 82, '↻ Refresh', self.refresh, True)
        self.line(27, 85, w-27, 85)
        self.text(29, 104, 'Make every reset count.', 28, C['text'], 'bold')
        self.text(30, 149, 'Read local usage · compare workloads · choose when to redeem saved resets',
                  11, C['muted'])
        self.pill(w-259, 113, 105, 36, 'Steady', self.mode=='steady',
                  lambda: self.switch_mode('steady'))
        self.pill(w-146, 113, 118, 36, 'Bursty', self.mode=='spiky',
                  lambda: self.switch_mode('spiky'), color=C['purple'] if self.mode=='spiky' else None)

    def left_column(self, x, w):
        five, week = self.last_observation()
        o = self.usage.latest
        # Card A — actual observed percentages.
        self.section(x, 188, w, 271)
        self.text(x+19, 205, 'LATEST LOCAL SNAPSHOT', 10, C['faint'], 'bold')
        is_demo = self.settings.demo
        age = (self.now - o.ts).total_seconds()/3600 if o else None
        if is_demo:
            status, statuscolor = 'DEMO', C['amber']
        elif age is None:
            status, statuscolor = 'NO DATA', C['pink']
        elif age > 24:
            status, statuscolor = 'STALE', C['amber']
        else:
            status, statuscolor = 'OBSERVED', C['teal']
        self.text(x+w-19, 205, status, 9, statuscolor, 'bold', 'ne')
        if o:
            ts = o.ts.astimezone().strftime('%d %b %Y, %H:%M')
            self.text(x+19, 229, f'Snapshot: {ts}  ·  local time', 10, C['muted'])
        else:
            self.text(x+19, 231, 'No rate-limit snapshots found in sessions', 10, C['muted'])
        self.line(x+19, 255, x+w-19, 255)
        for i,(name, val, reset, color) in enumerate((
              ('5-HOUR ALLOWANCE', five, o.five_end if o else None, C['teal']),
              ('WEEKLY ALLOWANCE', week, o.week_end if o else None, C['blue']))):
            y = 270 + i*91
            self.text(x+19, y, name, 9, C['muted'], 'bold')
            if val is None:
                self.text(x+w-19, y+14, '—', 19, C['faint'], 'bold', 'ne')
                label = 'No remaining estimate available'
            else:
                self.text(x+w-19, y+14, f'{100-val:.0f}% left', 19, color, 'bold', 'ne')
                label = f'{val:.1f}% used  ·  reset {local_time(reset)}'
            self.bar(x+19, y+42, w-38, (100-val) if val is not None else 0, color)
            self.text(x+19, y+55, label, 9, C['muted'])
        # Card B — minimal local-only settings.
        self.section(x, 474, w, 305)
        self.text(x+19, 492, 'YOUR PLANNING INPUTS', 10, C['faint'], 'bold')
        self.text(x+19, 523, 'Banked resets', 13, C['text'], 'bold')
        self.text(x+19, 545, 'Read the count in Settings → Usage', 9, C['muted'])
        self.small_button(x+w-126, 517, 31, '−', lambda: self.adjust_banked(-1))
        self.text(x+w-80, 533, str(self.settings.banked), 20, C['teal'], 'bold', 'center')
        self.small_button(x+w-50, 517, 31, '+', lambda: self.adjust_banked(+1))
        self.line(x+19, 573, x+w-19, 573)
        self.text(x+19, 587, 'Expiry', 11, C['muted'])
        self.text(x+19, 609, self.expiry.strftime('%d %b %Y') if self.expiry else 'Not specified', 11, C['text'], 'bold')
        self.small_button(x+w-91, 585, 72, 'Set date', self.set_expiry)
        self.line(x+19, 635, x+w-19, 635)
        self.text(x+19, 648, 'Daily usage rate', 11, C['muted'])
        self.text(x+19, 672, f'{self.daily:.1f} weekly pts / day', 13, C['text'], 'bold')
        self.small_button(x+w-86, 650, 67, 'Adjust', self.set_daily)
        self.text(x+19, 701, '5h/weekly cost ratio', 11, C['muted'])
        self.text(x+19, 726, f'{self.ratio:.2f} : 1', 13, C['text'], 'bold')
        self.small_button(x+w-86, 702, 67, 'Adjust', self.set_ratio)
        if self.settings.daily_override is None and self.usage.daily_rate is None:
            self.text(x+19, 755, 'Rate uses a fallback — tune for accuracy.', 9, C['amber'])
        elif self.settings.ratio_override is None and self.usage.ratio is None:
            self.text(x+19, 755, 'Ratio is assumed; calibrate with more logs.', 9, C['amber'])
        else:
            self.text(x+19, 755, 'Saved locally; never written to Codex.', 9, C['faint'])
        # Card C — clear and honest about the sources.
        self.section(x, 794, w, 182)
        self.text(x+19, 812, 'SOURCE & MOST-USED MODEL', 10, C['faint'], 'bold')
        self.text(x+19, 840, (self.usage.model or 'No model in metadata')[:31],
                  13, C['text'], 'bold')
        path = self.usage.folder if self.settings.demo else self.folder
        if len(path) > 44:
            path = '…' + path[-42:]
        self.text(x+19, 871, path, 10, C['muted'])
        self.text(x+19, 892, f'{self.usage.scanned_files} files · {len(self.usage.observations)} snapshots',
                  10, C['muted'])
        self.small_button(x+19, 930, w-38, 'Exit demo' if self.settings.demo else 'Preview with sample data',
                          self.toggle_demo)

    def right_column(self, x, w):
        self.text(x+2, 191, '21-DAY OUTLOOK', 13, C['text'], 'bold')
        view_options = (('calendar','Calendar',93), ('timeline','Timeline',91), ('month','Month',78))
        widths = sum(a[2] + 8 for a in view_options)-8
        xx = x+w-widths
        for key, name, ww in view_options:
            self.pill(xx, 185, ww, 34, name, self.view == key,
                      lambda value=key: self.switch_view(value), small=True)
            xx += ww + 8
        self.summary_cards(x, w)
        if self.view == 'calendar':
            bottom = self.calendar_view(x, w, 393, month=False)
        elif self.view == 'month':
            bottom = self.calendar_view(x, w, 393, month=True)
        else:
            bottom = self.timeline_view(x, w, 393)
        return self.recommendations(x, w, bottom+18)

    def summary_cards(self, x, w):
        g = 12
        a = (w-2*g)/3
        o = self.usage.latest
        upcoming = []
        if o:
            upcoming = [(stamp, name) for stamp, name in
                        ((o.five_end, '5-hour'), (o.week_end, 'Weekly'))
                        if stamp and stamp >= self.now]
            upcoming.sort(key=lambda s: s[0])
        first = upcoming[0] if upcoming else None
        boxes = [
            ('NEXT REPORTED RESET',
             local_time(first[0]) if first else 'Not reported',
             first[1] + ' window' if first else 'Refresh your local snapshot', C['blue']),
            ('SAVED RESET PICK',
             local_time(self.plan.chosen[0]) if self.plan.chosen else 'Hold for now',
             f'{len(self.plan.chosen)} proposed redemptions' if self.plan.chosen else 'No useful redemption found', C['teal']),
            ('ESTIMATED EXTRA USE',
             f'+{self.plan.gained:.1f} pts',
             'Weekly-equivalent, across 21 days', C['purple'])
        ]
        for i, (name, value, foot, accent) in enumerate(boxes):
            bx = x+i*(a+g)
            self.section(bx, 235, a, 137)
            self.rect(bx+16, 252, 4, 18, accent, 2)
            self.text(bx+28, 254, name, 9, C['faint'], 'bold')
            max_len = max(14, int(a/10))
            if len(value) > max_len:
                value = value[:max_len-1] + '…'
            self.text(bx+17, 285, value, 16 if a>=235 else 13, accent, 'bold')
            self.text(bx+17, 334, foot, 9, C['muted'], width=a-27)

    def calendar_view(self, x, w, y, month=False):
        if month:
            month_start = self.now.astimezone().date().replace(day=1)
            m = month_start.month - 1 + self.month_offset
            month_start = month_start.replace(year=month_start.year + m//12, month=m%12+1)
            start = month_start-timedelta(days=month_start.weekday())
            # Use full rows to include the end of the current month.
            lastday = calendar.monthrange(month_start.year, month_start.month)[1]
            total = (((month_start.replace(day=lastday)-start).days+1+6)//7)*7
            days = [start+timedelta(days=i) for i in range(total)]
            title = month_start.strftime('%B %Y')
            cell_h = 105
        else:
            start = self.now.astimezone().date()
            days = [start+timedelta(days=i) for i in range(21)]
            title = 'Next 3 weeks  ·  ' + start.strftime('%d %b') + ' – ' + days[-1].strftime('%d %b %Y')
            cell_h = 125
        self.text(x+2, y, title, 13, C['text'], 'bold')
        if month:
            self.small_button(x+w-84, y-5, 34, '‹', lambda: self.move_month(-1))
            self.small_button(x+w-40, y-5, 34, '›', lambda: self.move_month(+1))
        else:
            self.text(x+w, y+3, 'TEAL: REDEEM   •   BLUE: NATURAL   •   PURPLE: BURST',
                      8, C['muted'], anchor='ne')
        y += 33
        cellgap = 6
        cellw = (w-cellgap*6)/7
        labels = ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN') if month else None
        if month:
            for j, label in enumerate(labels):
                self.text(x+j*(cellw+cellgap)+5,y, label, 9, C['faint'], 'bold')
            y += 21
        days_data = {r['date']:r for r in self.plan.daily_rows}
        event_data = {}
        for e in self.plan.events:
            ed = e['ts'].astimezone().date()
            event_data.setdefault(ed, []).append(e)
        for idx, day in enumerate(days):
            col = idx%7
            row = idx//7
            xx = x+col*(cellw+cellgap)
            yy = y+row*(cell_h+cellgap)
            d = days_data.get(day)
            ev = event_data.get(day, [])
            selected = any(e['type']=='banked' for e in ev)
            natural = any(e['type']=='weekly' for e in ev)
            burst = d and d['demand'] > max(1., self.daily*1.8)
            off_month = month and day.month != month_start.month
            base = C['card'] if not off_month else '#111A2B'
            if selected:
                base = C['teal_dark']
            elif burst:
                base = C['purple_dark']
            elif natural:
                base = C['blue_dark']
            self.rect(xx, yy, cellw, cell_h, base, 11,
                      C['teal'] if selected else C['line'], width=1)
            self.text(xx+10, yy+11, day.strftime('%d'), 13,
                      C['faint'] if off_month else C['text'], 'bold')
            if not month:
                self.text(xx+10, yy+32, day.strftime('%a').upper(), 8, C['faint'], 'bold')
            if day == self.now.astimezone().date():
                self.rect(xx+cellw-30, yy+12, 20, 5, C['teal'], 3)
            if d:
                demandval = d['demand']
                color = C['purple'] if burst else C['teal']
                if selected: color = C['teal']
                if cellw>=91:
                    self.text(xx+9, yy+(53 if not month else 35),
                              f'{demandval:.1f} pts', 10, color, 'bold')
                self.bar(xx+9, yy+cell_h-19, cellw-18,
                         min(100, 100*d['demand']/max(1.,self.daily*3.2)),color,h=5)
                if d['demand'] > d['served'] + .1:
                    self.rect(xx+cellw-14, yy+11, 5, 5, C['pink'], 2)
            badges = []
            if selected: badges.append(('BANKED', C['teal']))
            if natural: badges.append(('WEEKLY', C['blue']))
            if burst and not selected: badges.append(('BURST', C['purple']))
            if not month and any(e['type']=='five' and e.get('observed') for e in ev):
                badges.append(('5H RESET', C['amber']))
            for bi, (label, tone) in enumerate(badges[:2]):
                py = yy + (75 if not month else 55) + bi * 16
                self.text(xx+9, py, '• '+label, 8, tone, 'bold')
            # Day inspection by a simple click keeps the interface minimal.
            if d or ev:
                self.hot(xx, yy, cellw, cell_h,
                         lambda dt=day: self.day_detail(dt), 'Inspect '+day.isoformat())
        bottom = y + (len(days)+6)//7 * (cell_h+cellgap)
        self.text(x+2, bottom+2, 'Each cell shows forecast daily demand in weekly-limit points. '
                  'A dot signals forecast capacity constraints.', 9, C['muted'])
        return bottom+23

    def day_detail(self, day):
        row = next((r for r in self.plan.daily_rows if r['date']==day), None)
        evs = [e for e in self.plan.events if e['ts'].astimezone().date() == day]
        text = f'{day:%A, %d %B %Y}\n\n'
        if row:
            text += (f'Forecast demand: {row["demand"]:.1f} weekly points\n'
                     f'Forecast served: {row["served"]:.1f} weekly points\n'
                     f'End-of-day weekly used: {row["end_week_used"]:.1f}%\n'
                     f'End-of-day five-hour used: {row["end_five_used"]:.1f}%\n')
        if evs:
            text += '\nEvents:\n'
            for e in evs[:12]:
                text += f' • {e["ts"].astimezone():%H:%M} — {e["label"]}\n'
        else:
            text += '\nNo reset events on this day.'
        messagebox.showinfo('Day details (forecast)', text, parent=self.root)

    def timeline_view(self, x, w, y):
        self.text(x+2, y, 'Projected demand vs usable capacity', 13, C['text'], 'bold')
        self.text(x+w, y+3, 'CLICK A ROW FOR DETAILS', 9, C['muted'], anchor='ne')
        y+=34
        self.section(x, y, w, 720)
        rows = self.plan.daily_rows
        max_d = max(1., max((r['demand'] for r in rows), default=1.))
        for i, r in enumerate(rows):
            yy=y+16+i*32
            day=r['date']
            self.text(x+13, yy+4, day.strftime('%a %d'), 10, C['muted'], 'bold')
            bar_x=x+90
            bw=w-213
            self.bar(bar_x, yy+7, bw, 100, C['card2'],h=12)
            if r['demand']:
                self.rect(bar_x, yy+7, bw*r['demand']/max_d, 12, C['purple_dark'], 5)
            if r['served']:
                self.rect(bar_x, yy+7, max(2,bw*r['served']/max_d), 12,
                          C['teal'] if self.mode=='steady' else C['purple'], 5)
            self.text(x+w-13, yy+3, f'{r["served"]:.1f} / {r["demand"]:.1f}',
                      10, C['text'], anchor='ne')
            self.hot(x+10, yy, w-20, 28,
                     lambda d=day: self.day_detail(d), 'Inspect '+day.isoformat())
        bottom=y+720
        self.text(x+4, bottom+9, 'Filled bar = usable projected demand · faint bar = total demand · units = weekly points',
                  9, C['muted'])
        return bottom+34

    def recommendations(self, x, w, y):
        self.section(x,y,w,186)
        self.text(x+18,y+16,'RECOMMENDED NEXT ACTION',10,C['faint'],'bold')
        if self.plan.chosen:
            when=self.plan.chosen[0].astimezone().strftime('%a %d %b, %H:%M')
            self.text(x+18,y+39, 'Redeem a saved reset · '+when, 16,C['teal'],'bold')
        elif self.settings.banked == 0:
            self.text(x+18,y+39,'Enter your saved reset count',16,C['teal'],'bold')
        else:
            self.text(x+18,y+39,'Keep your banked resets for later',16,C['teal'],'bold')
        self.text(x+18,y+74,self.plan.explanation,10,C['muted'],width=w-35)
        if self.mode=='steady':
            advice='Model plan: use a lightweight option for routine work; reserve demanding models for tasks that need them.'
        else:
            advice='Model plan: batch demanding work into high-intensity blocks; use lower-cost options for maintenance.'
        self.line(x+18,y+114,x+w-18,y+114)
        self.text(x+18,y+126,advice,10,C['text'],width=w-37)
        self.text(x+18,y+163,'A recommendation is not a redemption. Apply banked resets manually in your account.',9,C['faint'])
        y+=206
        self.section(x,y,w,123)
        self.text(x+18,y+13,'HOW TO READ THESE ESTIMATES',10,C['faint'],'bold')
        q = ('Uses local rate-limit snapshots, a daily workload estimate, and a forecast of 5-hour and weekly windows. '
             'Saved reset count and expiry are entered manually. Shared usage outside Codex logs may be missing; '
             'model costs and future activity vary. No traffic is sent or received.')
        self.text(x+18,y+40,q,10,C['muted'],width=w-35)
        return y+145

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    ResetApp().run()

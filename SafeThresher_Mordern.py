"""Safe Thresher: Bluetooth flood sensor monitor and trial calibration.
Requires: pip install -r requirements.txt   (see .env.example for API keys)
"""
import csv
import queue
import statistics
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None

# Optional cloud integration: retain full functionality when cloud_serv.py is present.
try:
    from cloud_serv import CloudServices
    CLOUD_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    CLOUD_AVAILABLE = False
    class CloudServices:
        def __init__(self, emit):
            self.emit = emit
            self.state = {}
            self.muted = True
        def start(self):
            self.emit('cloud_status', 'Cloud integrations unavailable: add cloud_serv.py and its dependencies')
        def stop(self): pass
        def log_reading(self, *args): pass
        def log_calibration(self, *args): pass
        def observe_zone(self, *args): pass
        def start_listening(self):
            self.emit('heard', 'Voice unavailable', 'Add cloud_serv.py and configure its API keys.')
        def stop_listening(self): pass
        def toggle_mute(self):
            self.muted = not self.muted
            return self.muted

APP_NAME = 'Safe Thresher'
BG = '#1a1a2e'
CARD_BG = '#16213e'
ACCENT = '#0f3460'
BTN_FG = '#e94560'
TEXT = '#eaeaea'
MUTED = '#8892a4'
SUCCESS = '#4ecca3'
WARN = '#f5a623'
DANGER = '#e94560'

# Extra UI tints derived from the base palette (visual only)
GRID = '#22304f'
BORDER = '#2d4263'
BLUE = '#4a90e2'
BLUE_HI = '#7ab8ff'
RED_HI = '#ff7a90'
FILL = '#213f4a'
BTN_HOVER = '#c93652'
BTN2_HOVER = '#164a82'

DATA_DIR = Path.home() / 'Documents' / 'Safe Thresher Data'
DATA_DIR.mkdir(parents=True, exist_ok=True)
READINGS_CSV = DATA_DIR / 'sensor_readings.csv'
TRIALS_CSV = DATA_DIR / 'calibration_trials.csv'


def append_csv(path, fields, row):
    exists = path.exists() and path.stat().st_size > 0
    with path.open('a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


class SafeThresherApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME + ' | Dynamic Flood Monitor')
        self.root.geometry('1180x850')
        self.root.minsize(950, 740)
        self.root.configure(bg=BG)
        self.events = queue.Queue()
        self.port = None
        self.worker = None
        self.running = False
        self.current = None
        self.max_seen = None
        self.recent = deque(maxlen=15)
        self.chart_history = deque(maxlen=140)
        self.calibrating = False
        self.cal_start = 0.0
        self.cal_samples = []
        self.calibration_number = 0
        self.thresholds = None
        self.reference = None
        self.flutter = None
        self.log_counter = 0
        self.sync_var = tk.StringVar(value='NOT SYNCED')
        self.device_var = tk.StringVar(value='Arduino thresholds: unknown')
        self.pending_sync = None
        self.sync_attempts = 0
        self.sync_deadline = 0
        self.last_device_thresholds = None
        self.last_status_request = 0
        self.serial_lock = threading.Lock()

        self.port_var = tk.StringVar(value='COM11')
        self.baud_var = tk.StringVar(value='9600')
        self.status_var = tk.StringVar(value='Disconnected')
        self.live_var = tk.StringVar(value='---')
        self.max_var = tk.StringVar(value='---')
        self.median_var = tk.StringVar(value='---')
        self.cal_var = tk.StringVar(value='Ready to calibrate')
        self.ref_var = tk.StringVar(value='---')
        self.flutter_var = tk.StringVar(value='---')
        self.threshold_var = tk.StringVar(value='Not calibrated')
        self.alert_var = tk.StringVar(value='Awaiting readings')
        self._pulse_on = True

        # Cloud layer: Tiger Data + Gemini + ElevenLabs (all on background threads)
        self.ai_var = tk.StringVar(value='AI analyst waiting for data...')
        self.cloud_var = tk.StringVar(value='Cloud: starting...')
        self.heard_var = tk.StringVar(value='Hold the mic button and ask a question')
        self.cloud = CloudServices(emit=lambda *a: self.events.put(a))

        self.build_ui()
        self.root.after(75, self.poll_events)
        self.root.after(100, self.tick)
        self.root.after(700, self.pulse)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.cloud.start()

    # ------------------------------------------------------------------
    # UI helpers (visual only)
    # ------------------------------------------------------------------
    def label(self, parent, text=None, size=10, variable=None, color=TEXT, bold=False, bg=None, **kwargs):
        opts = dict(font=('Segoe UI', size, 'bold' if bold else 'normal'), fg=color,
                    bg=bg or parent.cget('bg'), anchor='w')
        if variable is not None:
            opts['textvariable'] = variable
        else:
            opts['text'] = text
        opts.update(kwargs)
        return tk.Label(parent, **opts)

    def panel(self, parent, padx=18, pady=16, accent=None):
        frame = tk.Frame(parent, bg=CARD_BG, padx=padx, pady=pady,
                         highlightbackground=BORDER, highlightthickness=1)
        # thin accent strip along the top edge
        tk.Frame(frame, bg=accent or ACCENT, height=2).place(x=0, y=0, relwidth=1)
        return frame

    def action(self, parent, text, command, primary=False):
        base = BTN_FG if primary else ACCENT
        hover = BTN_HOVER if primary else BTN2_HOVER
        btn = tk.Button(parent, text=text, command=command, cursor='hand2',
                        bg=base, fg=TEXT, activebackground=hover,
                        activeforeground='white', relief='flat', bd=0,
                        highlightthickness=1,
                        highlightbackground=BTN_FG if primary else BORDER,
                        highlightcolor=SUCCESS,
                        font=('Segoe UI', 9, 'bold'), padx=18, pady=10)
        btn.bind('<Enter>', lambda e: btn.config(bg=hover) if str(btn['state']) != 'disabled' else None)
        btn.bind('<Leave>', lambda e: btn.config(bg=base) if str(btn['state']) != 'disabled' else None)
        return btn

    def section(self, parent, num, title, subtitle=None):
        row = tk.Frame(parent, bg=parent.cget('bg'))
        row.pack(fill='x')
        self.label(row, num, 11, color=BTN_FG, bold=True,
                   font=('Consolas', 12, 'bold')).pack(side='left')
        self.label(row, '   ' + title, 11, bold=True).pack(side='left')
        tk.Frame(row, bg=BORDER, height=1).pack(side='left', fill='x', expand=True, padx=(14, 0), pady=(3, 0))
        if subtitle:
            self.label(parent, subtitle, 9, color=MUTED).pack(anchor='w', pady=(5, 12))

    def tint_status(self, *_):
        text = self.status_var.get()
        if text.startswith('Connected'):
            color = SUCCESS
        elif text.startswith('Connection lost') or 'error' in text.lower():
            color = DANGER
        else:
            color = MUTED
        self.status_label.config(fg=color)
        self.status_dot.config(fg=color)

    def tint_sync(self, *_):
        text = self.sync_var.get().upper()
        if 'MISMATCH' in text or 'FAILED' in text or 'REJECTED' in text:
            color = DANGER
        elif 'SYNCED / VERIFIED' in text and 'NOT' not in text:
            color = SUCCESS
        else:
            color = WARN
        self.sync_label.config(fg=color)

    def tint_alert(self, *_):
        text = self.alert_var.get().upper()
        if text.startswith('RED'):
            color = DANGER
        elif text.startswith('BLUE'):
            color = BLUE_HI
        elif text.startswith('GREEN'):
            color = SUCCESS
        else:
            color = WARN
        self.alert_label.config(fg=color)
        self.alert_dot.config(fg=color)

    def pulse(self):
        self._pulse_on = not self._pulse_on
        if self.running:
            self.live_dot.config(fg=SUCCESS if self._pulse_on else '#2a7a64')
            self.live_text.config(text='LIVE TELEMETRY', fg=SUCCESS)
        else:
            self.live_dot.config(fg=MUTED)
            self.live_text.config(text='STANDBY', fg=MUTED)
        self.root.after(700, self.pulse)

    def build_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('Navy.Horizontal.TProgressbar', troughcolor=BG,
                        background=SUCCESS, bordercolor=BG, lightcolor=SUCCESS,
                        darkcolor=SUCCESS, thickness=8)
        style.configure('Tech.Vertical.TScrollbar', background=ACCENT, troughcolor=BG,
                        bordercolor=BG, arrowcolor=MUTED, lightcolor=ACCENT,
                        darkcolor=ACCENT, gripcount=0)
        style.map('Tech.Vertical.TScrollbar', background=[('active', BORDER)])

        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill='both', expand=True)
        header = tk.Frame(shell, bg=BG, padx=28, pady=18)
        header.pack(fill='x')
        brand = tk.Frame(header, bg=BG)
        brand.pack(side='left')
        title_row = tk.Frame(brand, bg=BG)
        title_row.pack(anchor='w')
        self.label(title_row, '◈', 26, color=BTN_FG, bold=True).pack(side='left', padx=(0, 10))
        self.label(title_row, 'SAFE', 24, bold=True).pack(side='left')
        self.label(title_row, ' THRESHER', 24, color=SUCCESS, bold=True).pack(side='left')
        self.label(brand, 'NAVAL SYSTEMS  ·  WATER INTRUSION MONITORING', 9,
                   color=MUTED, bold=True).pack(anchor='w', pady=(3, 0))
        badge = tk.Frame(header, bg=ACCENT, padx=16, pady=10,
                         highlightbackground=BORDER, highlightthickness=1)
        badge.pack(side='right')
        self.live_dot = self.label(badge, '●', 11, color=MUTED, bold=True)
        self.live_dot.pack(side='left', padx=(0, 8))
        self.live_text = self.label(badge, 'STANDBY', 10, color=MUTED, bold=True)
        self.live_text.pack(side='left')

        tk.Frame(shell, bg=BTN_FG, height=2).pack(fill='x')
        tk.Frame(shell, bg=ACCENT, height=1).pack(fill='x')

        # Scrollable workspace keeps controls reachable on smaller laptop screens.
        viewport = tk.Canvas(shell, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient='vertical', command=viewport.yview,
                                  style='Tech.Vertical.TScrollbar')
        viewport.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        viewport.pack(side='left', fill='both', expand=True)
        workspace = tk.Frame(viewport, bg=BG, padx=28, pady=20)
        window_id = viewport.create_window((0, 0), window=workspace, anchor='nw')
        workspace.bind('<Configure>', lambda e: viewport.configure(scrollregion=viewport.bbox('all')))
        viewport.bind('<Configure>', lambda e: viewport.itemconfigure(window_id, width=e.width))
        viewport.bind('<Enter>', lambda e: viewport.bind_all('<MouseWheel>', lambda ev: viewport.yview_scroll(int(-ev.delta / 120), 'units')))
        viewport.bind('<Leave>', lambda e: viewport.unbind_all('<MouseWheel>'))

        connection = self.panel(workspace, pady=12, accent=SUCCESS)
        connection.pack(fill='x', pady=(0, 16))
        self.label(connection, '⌁  COMMUNICATIONS LINK', 10, color=MUTED, bold=True).pack(side='left', padx=(0, 20))
        for name, var, width in [('PORT', self.port_var, 8), ('BAUD', self.baud_var, 7)]:
            self.label(connection, name, 9, color=MUTED, bold=True).pack(side='left', padx=(0, 6))
            tk.Entry(connection, textvariable=var, width=width, bg=BG, fg=SUCCESS,
                     insertbackground=SUCCESS, relief='flat', font=('Consolas', 11, 'bold'),
                     highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=SUCCESS).pack(side='left', ipady=6, padx=(0, 14))
        self.action(connection, 'CONNECT', self.connect, True).pack(side='left', padx=(0, 7))
        self.action(connection, 'DISCONNECT', self.disconnect).pack(side='left')
        self.status_label = self.label(connection, variable=self.status_var, size=9, color=MUTED, bold=True)
        self.status_label.pack(side='right', padx=(6, 0))
        self.status_dot = self.label(connection, '●', 9, color=MUTED)
        self.status_dot.pack(side='right')

        metrics = tk.Frame(workspace, bg=BG)
        metrics.pack(fill='x', pady=(0, 16))
        for i, (heading, var, tint, sub) in enumerate([
            ('LIVE SENSOR / ADC', self.live_var, TEXT, 'Raw input · analog A1'),
            ('PEAK READING', self.max_var, BTN_FG, 'Retained until manual clear'),
            ('FILTERED MEDIAN', self.median_var, SUCCESS, 'Rolling 15-sample median')]):
            card = self.panel(metrics, padx=22, pady=18, accent=tint if tint != TEXT else BLUE)
            card.grid(row=0, column=i, sticky='nsew', padx=(0, 12) if i < 2 else 0)
            metrics.columnconfigure(i, weight=1, uniform='metrics')
            tk.Frame(card, bg=tint if tint != TEXT else BLUE, width=3).place(x=0, y=10, width=3, height=44)
            self.label(card, heading, 9, color=MUTED, bold=True).pack(anchor='w')
            self.label(card, variable=var, size=38, color=tint, bold=True,
                       font=('Consolas', 38, 'bold')).pack(anchor='w', pady=(4, 0))
            self.label(card, sub, 9, color=MUTED).pack(anchor='w')
        self.action(workspace, 'RESET PEAK', self.clear_max).pack(anchor='e', pady=(0, 12))

        center = tk.Frame(workspace, bg=BG)
        center.pack(fill='both', expand=True, pady=(0, 16))
        center.columnconfigure(0, weight=3)
        center.columnconfigure(1, weight=2)

        telemetry = self.panel(center, accent=SUCCESS)
        telemetry.grid(row=0, column=0, sticky='nsew', padx=(0, 14))
        self.section(telemetry, '01', 'LIVE SIGNAL', 'Real-time ADC samples · 0–1023')
        self.chart = tk.Canvas(telemetry, height=240, bg=BG, highlightthickness=1,
                               highlightbackground=GRID)
        self.chart.pack(fill='both', expand=True)
        self.label(telemetry, 'SYSTEM ASSESSMENT', 9, color=MUTED, bold=True).pack(anchor='w', pady=(15, 5))
        assess = tk.Frame(telemetry, bg=CARD_BG)
        assess.pack(fill='x')
        self.alert_dot = self.label(assess, '●', 14, color=WARN)
        self.alert_dot.pack(side='left', padx=(0, 10))
        self.alert_label = self.label(assess, variable=self.alert_var, size=11,
                                      color=WARN, bold=True, wraplength=520, justify='left')
        self.alert_label.pack(side='left', anchor='w')

        calibration = self.panel(center, accent=BTN_FG)
        calibration.grid(row=0, column=1, sticky='nsew')
        self.section(calibration, '02', 'CALIBRATION', 'ONE-STEP MAXIMUM CALIBRATION · 250 mL')
        self.label(calibration, 'Fill to the maximum test level, allow water to settle, then capture 10 seconds. Highest recorded ADC becomes MAX.',
                   10, color=TEXT, wraplength=340, justify='left').pack(anchor='w', pady=(0, 16))
        self.progress = ttk.Progressbar(calibration, maximum=10, mode='determinate',
                                        style='Navy.Horizontal.TProgressbar')
        self.progress.pack(fill='x', pady=(0, 12))
        self.label(calibration, variable=self.cal_var, size=10, color=SUCCESS,
                   wraplength=340, justify='left').pack(anchor='w', pady=(0, 18))
        self.cal_button = self.action(calibration, 'START 10-SECOND CALIBRATION',
                                      self.start_calibration, True)
        self.cal_button.pack(fill='x', pady=(0, 9))
        self.action(calibration, 'RESEND THRESHOLDS TO ARDUINO',
                    self.resend_thresholds).pack(fill='x', pady=(0, 9))
        self.action(calibration, 'RESET CALIBRATION  /  BEEPER OFF',
                    self.reset_calibration).pack(fill='x')

        results = self.panel(workspace, accent=BLUE)
        results.pack(fill='x', pady=(0, 14))
        self.section(results, '03', 'DYNAMIC THRESHOLD ENGINE',
                     'MAX = highest reading during calibration. Blue blink MAX−65, red MAX−35, critical MAX−21.')
        self.zone_bar = tk.Canvas(results, height=58, bg=CARD_BG, highlightthickness=0)
        self.zone_bar.pack(fill='x', pady=(0, 12))
        self.zone_bar.bind('<Configure>', lambda e: self.draw_zone_bar())
        self.label(results, variable=self.ref_var, size=10).pack(anchor='w', pady=(0, 5))
        self.label(results, variable=self.flutter_var, size=10, color=MUTED).pack(anchor='w', pady=(0, 10))
        self.label(results, variable=self.threshold_var, size=11, color=SUCCESS,
                   font=('Consolas', 11), justify='left').pack(anchor='w')
        diagnostics = self.panel(workspace, accent=WARN)
        diagnostics.pack(fill='x', pady=(0, 14))
        self.section(diagnostics, '04', 'ARDUINO LINK & SYSTEM LOG')
        sync_row = tk.Frame(diagnostics, bg=CARD_BG)
        sync_row.pack(fill='x', pady=(12, 3))
        self.sync_label = self.label(sync_row, variable=self.sync_var, size=12, color=WARN, bold=True)
        self.sync_label.pack(anchor='w')
        self.label(diagnostics, variable=self.device_var, size=10, color=MUTED,
                   font=('Consolas', 10)).pack(anchor='w', pady=(0, 10))
        buttons = tk.Frame(diagnostics, bg=CARD_BG)
        buttons.pack(fill='x', pady=(0, 10))
        self.action(buttons, 'VERIFY ARDUINO THRESHOLDS', self.request_status).pack(side='left', padx=(0, 8))
        self.action(buttons, 'CLEAR LOG', lambda: self.log_box.delete('1.0', 'end')).pack(side='left')
        self.log_box = tk.Text(diagnostics, height=9, bg=BG, fg=TEXT, insertbackground=TEXT,
                               relief='flat', font=('Consolas', 10), wrap='word', state='normal',
                               padx=12, pady=10, highlightthickness=1,
                               highlightbackground=GRID, highlightcolor=BORDER,
                               selectbackground=ACCENT)
        self.log_box.pack(fill='x')
        for tag, col in [('ts', MUTED), ('SYSTEM', MUTED), ('LINK', BLUE_HI), ('TX', BLUE_HI),
                         ('RX', TEXT), ('CAL', WARN), ('RETRY', WARN), ('WARNING', WARN),
                         ('SUCCESS', SUCCESS), ('ERROR', DANGER)]:
            self.log_box.tag_configure(tag, foreground=col)
        self.log('SYSTEM', 'Ready. Connect HC-05, calibrate, and verify Arduino acknowledgment.')

        self.label(workspace, 'DATA LOGS  /  ' + str(DATA_DIR), 9, color=MUTED,
                   wraplength=1000).pack(anchor='w')
        self.label(workspace, '⚠  PROTOTYPE ONLY  •  Thresholds are sensor-signal bands, not validated water-depth percentages or a certified flood alarm.',
                   9, color=WARN, wraplength=1000).pack(anchor='w', pady=(7, 10))

        self.status_var.trace_add('write', self.tint_status)
        self.sync_var.trace_add('write', self.tint_sync)
        self.alert_var.trace_add('write', self.tint_alert)
        self.tint_status()
        self.tint_sync()
        self.tint_alert()
        self.draw_chart()

    def draw_chart(self):
        if not hasattr(self, 'chart'):
            return
        canvas = self.chart
        canvas.delete('all')
        w = max(canvas.winfo_width(), 300)
        h = max(canvas.winfo_height(), 180)
        left, right, top, bottom = 46, w - 16, 14, h - 26

        def yv(v):
            return bottom - (min(max(v, 0), 1023) / 1023) * (bottom - top)

        values = list(getattr(self, 'chart_history', []))
        coords = []
        if len(values) >= 2:
            for i, v in enumerate(values):
                x = left + (right - left) * i / max(1, len(values) - 1)
                coords.extend((x, yv(v)))
            # soft area fill beneath the trace
            canvas.create_polygon(*coords, coords[-2], bottom, coords[0], bottom,
                                  fill=FILL, outline='')

        for i in range(1, 7):
            x = left + (right - left) * i / 7
            canvas.create_line(x, top, x, bottom, fill='#1c2846')
        for v in (0, 256, 512, 768, 1023):
            y = yv(v)
            canvas.create_line(left, y, right, y, fill=GRID, dash=(2, 6))
            canvas.create_text(left - 8, y, text=str(v), fill=MUTED, anchor='e', font=('Consolas', 8))
        canvas.create_line(left, top, left, bottom, fill=BORDER)
        canvas.create_line(left, bottom, right, bottom, fill=BORDER)

        if self.thresholds:
            for val, col, name in zip(self.thresholds, (BLUE, BLUE_HI, DANGER, RED_HI),
                                      ('BLUE', 'BLINK', 'RED', 'CRIT')):
                y = yv(val)
                canvas.create_line(left, y, right, y, fill=col, dash=(6, 4))
                canvas.create_text(right - 4, y - 7, text=f'{name} {val}', fill=col,
                                   anchor='e', font=('Consolas', 8, 'bold'))
        if self.max_seen is not None:
            y = yv(self.max_seen)
            canvas.create_line(left, y, left + 70, y, fill=BTN_FG, width=2)
            canvas.create_text(left + 76, y, text=f'PEAK {self.max_seen}', fill=BTN_FG,
                               anchor='w', font=('Consolas', 8, 'bold'))

        if len(values) >= 2:
            canvas.create_line(*coords, fill='#1f6b5a', width=6, capstyle='round', joinstyle='round')
            canvas.create_line(*coords, fill=SUCCESS, width=2, capstyle='round', joinstyle='round')
            cx, cy = coords[-2], coords[-1]
            canvas.create_oval(cx - 8, cy - 8, cx + 8, cy + 8, outline=BTN_FG)
            canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=BTN_FG, outline=TEXT)
            canvas.create_text(cx - 14, max(cy - 14, top + 6), text=str(values[-1]), fill=TEXT,
                               anchor='e', font=('Consolas', 10, 'bold'))
        else:
            canvas.create_text(w / 2, h / 2, text='◌  AWAITING SENSOR TELEMETRY',
                               fill=MUTED, font=('Consolas', 11))
        self.draw_zone_bar()

    def draw_zone_bar(self):
        if not hasattr(self, 'zone_bar'):
            return
        bar = self.zone_bar
        bar.delete('all')
        w = max(bar.winfo_width(), 300)
        left, right = 2, w - 2
        y0, y1 = 8, 28

        def xv(v):
            return left + (min(max(v, 0), 1023) / 1023) * (right - left)

        if not self.thresholds:
            bar.create_rectangle(left, y0, right, y1, fill=BG, outline=BORDER)
            bar.create_text(w / 2, (y0 + y1) / 2, text='AWAITING CALIBRATION',
                            fill=MUTED, font=('Consolas', 9, 'bold'))
            return
        a, b, c, d = self.thresholds
        edges = [0, a, b, c, d, 1023]
        colors = [SUCCESS, BLUE, BLUE_HI, DANGER, RED_HI]
        for i, col in enumerate(colors):
            x0, x1 = xv(edges[i]), xv(edges[i + 1])
            if x1 - x0 > 1:
                bar.create_rectangle(x0 + 1, y0, x1 - 1, y1, fill=col, outline='')
        for v in (a, b, c, d):
            bar.create_text(xv(v), y1 + 11, text=str(v), fill=MUTED, font=('Consolas', 8))
        if self.recent:
            m = statistics.median(self.recent)
            x = xv(m)
            bar.create_polygon(x - 5, y0 - 7, x + 5, y0 - 7, x, y0 + 1, fill=TEXT, outline='')
            bar.create_line(x, y0, x, y1, fill=TEXT, width=2)

    def log(self, category, message):
        if not hasattr(self, 'log_box'):
            return
        self.log_box.insert('end', f"[{datetime.now().strftime('%H:%M:%S')}] ", 'ts')
        self.log_box.insert('end', f"{category}: ", category)
        self.log_box.insert('end', f"{message}\n")
        self.log_box.see('end')

    # ------------------------------------------------------------------
    # Backend
    # ------------------------------------------------------------------
    def send_command(self, command):
        if not self.running or self.port is None:
            raise ConnectionError('Bluetooth disconnected')
        with self.serial_lock:
            self.port.write((command + '\n').encode('ascii'))
            self.port.flush()
        self.log('TX', command)

    def request_status(self):
        try:
            self.send_command('STATUS')
            self.last_status_request = time.monotonic()
        except Exception as exc:
            self.log('ERROR', str(exc))

    def handle_protocol(self, line):
        self.log('RX', line)
        if line == 'ACK,CAL':
            self.sync_var.set('ACK RECEIVED — verifying Arduino thresholds...')
            self.request_status()
        elif line.startswith('STATUS,'):
            try:
                values = tuple(int(v) for v in line.split(',')[1:5])
                if len(values) != 4:
                    raise ValueError('Expected four values')
            except ValueError:
                self.log('ERROR', 'Malformed STATUS response')
                return
            self.last_device_thresholds = values
            self.device_var.set('Arduino thresholds: ' + ', '.join(map(str, values)))
            if self.thresholds is not None and values == tuple(self.thresholds) and self.pending_sync is not None:
                self.pending_sync = None
                self.sync_var.set('CALIBRATION SYNCED / VERIFIED')
                self.log('SUCCESS', 'Arduino thresholds match Python calibration.')
            else:
                self.sync_var.set('THRESHOLDS NOT VERIFIED / NOT ARMED')
                self.log('WARNING', 'Arduino thresholds do not match app thresholds.')
        elif line == 'ACK,CAL_START':
            self.sync_var.set('CALIBRATING — BUZZER DISARMED')
        elif line == 'ACK,RESET':
            self.sync_var.set('BEEPER OFF — AWAITING FIRST CALIBRATION')
            self.log('SUCCESS', 'Arduino confirmed reset; beeper silenced.')
            self.last_device_thresholds = None
        elif line.startswith('ERR,'):
            self.sync_var.set('ARDUINO REJECTED COMMAND')

    def connect(self):
        if self.running:
            return
        if serial is None:
            messagebox.showerror('Missing package', 'Install pyserial first:\npython -m pip install pyserial')
            return
        try:
            baud = int(self.baud_var.get())
            self.port = serial.Serial(self.port_var.get().strip(), baud, timeout=0.25, write_timeout=1)
        except Exception as e:
            messagebox.showerror('Bluetooth connection failed', str(e) + '\n\nCheck that the HC-05 is paired and no other app has this COM port open.')
            return
        self.running = True
        self.status_var.set('Connected to ' + self.port_var.get())
        self.sync_var.set('CONNECTED — thresholds not verified')
        self.log('LINK', 'Connected to ' + self.port_var.get())
        self.worker = threading.Thread(target=self.reader, daemon=True)
        self.worker.start()
        self.root.after(650, self.request_status)
        self.root.after(900, self.silence_if_uncalibrated)

    def silence_if_uncalibrated(self):
        # Keep the beeper off until the first calibration has been sent.
        if self.running and self.thresholds is None:
            self.silence_beeper()

    def silence_beeper(self):
        try:
            self.send_command('RESET')
        except Exception as exc:
            self.log('ERROR', 'Beeper-off send failed: ' + str(exc))

    def reset_calibration(self):
        # Cancel any capture in progress
        self.calibrating = False
        self.cal_button.config(state='normal')
        self.progress['value'] = 0
        self.cal_samples = []
        # Forget the app-side calibration
        self.thresholds = None
        self.reference = None
        self.flutter = None
        self.pending_sync = None
        self.sync_attempts = 0
        self.ref_var.set('---')
        self.flutter_var.set('---')
        self.threshold_var.set('Not calibrated')
        self.cal_var.set('Calibration reset — beeper off. Ready to calibrate')
        self.log('CAL', 'Calibration reset by user')
        # Tell the Arduino to turn the beeper off and drop calibrated thresholds
        if self.running:
            self.silence_beeper()
            self.sync_var.set('RESET SENT — AWAITING ARDUINO ACK')
        else:
            self.sync_var.set('RESET LOCALLY — ARDUINO NOT CONNECTED')
        self.update_alert()

    def reader(self):
        while self.running:
            try:
                line = self.port.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue
                try:
                    value = int(line)
                except ValueError:
                    self.events.put(('protocol', line))
                    continue
                if 0 <= value <= 1023:
                    self.events.put(('reading', value, time.monotonic(), datetime.now().isoformat(timespec='milliseconds')))
            except Exception as e:
                if self.running:
                    self.events.put(('error', str(e)))
                break

    def disconnect(self):
        self.running = False
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None
        self.status_var.set('Disconnected')
        self.sync_var.set('DISCONNECTED — NOT VERIFIED')
        self.pending_sync = None
        self.log('LINK', 'Disconnected')
        if self.calibrating:
            self.calibrating = False
            self.cal_var.set('Calibration interrupted — reconnect and retry')
            self.cal_button.config(state='normal')

    def clear_max(self):
        self.max_seen = None
        self.max_var.set('---')

    def start_calibration(self):
        if not self.running:
            messagebox.showwarning('Not connected', 'Connect to the HC-05 before calibrating.')
            return
        self.calibrating = True
        self.sync_var.set('CALIBRATING — ALARM DISARM REQUESTED')
        self.log('CAL', 'Started one-step 10-second MAX calibration')
        self.cal_start = time.monotonic()
        self.cal_samples = []
        self.progress['value'] = 0
        self.cal_button.config(state='disabled')
        self.cal_var.set('Capturing MAX: 10.0 seconds left')
        try:
            self.send_command('CAL_START')  # disarms and silences the buzzer
        except Exception as exc:
            self.log('ERROR', 'CAL_START send failed: ' + str(exc))

    def poll_events(self):
        for _ in range(300):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'protocol':
                self.handle_protocol(event[1])
                continue
            if event[0] == 'error':
                self.disconnect()
                self.status_var.set('Connection lost: ' + event[1])
                break
            if event[0] == 'cloud_log':
                self.log(event[1], event[2])
                continue
            if event[0] == 'cloud_status':
                self.cloud_var.set(event[1])
                continue
            if event[0] == 'ai':
                self.show_ai(event[1])
                continue
            if event[0] == 'heard':
                self.heard_var.set(f'You: {event[1]}\nAI: {event[2]}')
                continue
            _, value, sampled_at, timestamp = event
            self.current = value
            self.live_var.set(str(value))
            self.max_seen = value if self.max_seen is None else max(self.max_seen, value)
            self.max_var.set(str(self.max_seen))
            self.recent.append(value)
            self.chart_history.append(value)
            self.cloud.log_reading(value, self.calibrating)
            self.median_var.set(str(round(statistics.median(self.recent))))
            if self.calibrating and self.cal_start <= sampled_at <= self.cal_start + 10:
                self.cal_samples.append(value)
            self.log_counter += 1
            if self.log_counter % 5 == 0:
                try:
                    append_csv(READINGS_CSV, ['timestamp', 'adc_value', 'calibrating'], {
                        'timestamp': timestamp, 'adc_value': value, 'calibrating': int(self.calibrating)})
                except OSError as e:
                    self.status_var.set('CSV write error: ' + str(e))
            self.update_alert()
        self.draw_chart()
        self.root.after(75, self.poll_events)

    def tick(self):
        if self.calibrating:
            elapsed = time.monotonic() - self.cal_start
            self.progress['value'] = min(10, elapsed)
            self.cal_var.set(f'Capturing MAX: {max(0, 10 - elapsed):.1f} seconds left  |  {len(self.cal_samples)} samples')
            if elapsed >= 10:
                self.finish_calibration()
        if self.pending_sync is not None and self.running and time.monotonic() >= self.sync_deadline:
            if self.sync_attempts < 3:
                self.log('RETRY', f'No matching STATUS yet; resend {self.sync_attempts + 1}/3')
                self._transmit_thresholds()
            else:
                self.log('ERROR', 'No verified calibration after 3 attempts. Check HC-05 wiring and firmware.')
                self.sync_var.set('SYNC FAILED — NO VERIFIED RESPONSE')
                self.pending_sync = None
        self.root.after(100, self.tick)

    def finish_calibration(self):
        self.calibrating = False
        self.cal_button.config(state='normal')
        values = self.cal_samples
        if len(values) < 30:
            self.cal_var.set(f'Calibration failed: only {len(values)} samples. Check Bluetooth and retry.')
            self.silence_beeper()
            return

        # MAX is the highest REAL reading in this calibration, not the graph axis limit.
        measured_max = max(values)
        med = statistics.median(values)
        deviations = [abs(x - med) for x in values]
        mad = statistics.median(deviations)
        flutter = max(1, round(1.4826 * mad))

        # User-selected fixed ADC margins measured DOWN from the observed MAX.
        blue_blink = measured_max - 65
        red_solid = measured_max - 35
        red_blink = measured_max - 21

        # Green boundary is provisional until a dry baseline is measured.
        # Approximately 300 when MAX=647; never used to calculate upper margins.
        blue_solid = round(measured_max * (300 / 647))
        boundaries = [blue_solid, blue_blink, red_solid, red_blink]
        if not (0 < boundaries[0] < boundaries[1] < boundaries[2] < boundaries[3]):
            self.cal_var.set('Calibration failed: MAX too low for the requested 65/35/21 ADC margins.')
            self.log('ERROR', f'MAX={measured_max} cannot produce ordered warning zones.')
            self.silence_beeper()
            return

        self.reference = measured_max
        self.flutter = flutter
        self.thresholds = boundaries
        self.calibration_number += 1
        self.ref_var.set(f'CALIBRATED MAX: {measured_max} ADC   |   Median: {med:.1f}   |   Minimum: {min(values)}')
        self.flutter_var.set(f'Observed flutter: ±{flutter} ADC   |   Critical begins MAX − 21   |   Samples: {len(values)}')
        self.threshold_var.set(self.threshold_text())
        self.cal_var.set(f'Calibration #{self.calibration_number} complete. MAX locked at {measured_max}.')
        try:
            append_csv(TRIALS_CSV, ['timestamp', 'trial', 'water_ml', 'samples', 'minimum', 'maximum', 'median', 'mad', 'flutter', 'critical_margin', 'critical_margin_percent', 'green_start', 'blue_start', 'blue_blink_start', 'red_solid_start', 'red_blink_start'], {
                'timestamp': datetime.now().isoformat(timespec='seconds'),
                'trial': self.calibration_number, 'water_ml': 250, 'samples': len(values),
                'minimum': min(values), 'maximum': measured_max, 'median': med,
                'mad': mad, 'flutter': flutter, 'critical_margin': 21,
                'critical_margin_percent': round(2100 / measured_max, 2),
                'green_start': 0, 'blue_start': boundaries[0],
                'blue_blink_start': boundaries[1], 'red_solid_start': boundaries[2],
                'red_blink_start': boundaries[3]})
        except OSError as e:
            self.status_var.set('CSV write error: ' + str(e))
        self.cloud.log_calibration(self.calibration_number, measured_max, med, flutter, boundaries)
        self.log('CAL', f'MAX={measured_max}, median={med}, flutter=±{flutter}, thresholds={boundaries}')
        self.resend_thresholds()
        self.update_alert()

    def threshold_text(self):
        a, b, c, d = self.thresholds
        return (f'GREEN solid   below {a} (provisional dry boundary)\n'
                f'BLUE solid    {a}–{b - 1}\n'
                f'BLUE blinking {b}–{c - 1}  (MAX − 65)\n'
                f'RED solid     {c}–{d - 1}  (MAX − 35)\n'
                f'RED blinking  {d} and above  (MAX − 21; calibrated MAX={self.reference})')

    def _transmit_thresholds(self):
        try:
            command = 'CAL,' + ','.join(map(str, self.pending_sync))
            self.send_command(command)
            self.sync_attempts += 1
            self.sync_deadline = time.monotonic() + 2.5
            self.sync_var.set(f'AWAITING ARDUINO ACK / VERIFY — attempt {self.sync_attempts}/3')
        except Exception as exc:
            self.log('ERROR', 'Calibration send failed: ' + str(exc))
            self.sync_var.set('CALIBRATION SEND FAILED')
            self.pending_sync = None

    def resend_thresholds(self):
        if self.thresholds is None:
            messagebox.showinfo('Not calibrated', 'Run the one-step MAX calibration first.')
            return
        if not self.running:
            self.sync_var.set('NOT CONNECTED — CALIBRATION NOT SENT')
            self.log('WARNING', 'Connect Bluetooth before sending thresholds')
            return
        self.pending_sync = tuple(self.thresholds)
        self.sync_attempts = 0
        self._transmit_thresholds()

    def zone_of(self, value):
        a, b, c, d = self.thresholds
        if value < a:
            return 'GREEN'
        if value < b:
            return 'BLUE'
        if value < c:
            return 'BLUE_BLINK'
        if value < d:
            return 'RED'
        return 'RED_BLINK'

    def update_alert(self):
        if self.current is None:
            return
        if self.thresholds is None:
            self.alert_var.set('UNCALIBRATED — warning outputs disarmed with updated Arduino firmware')
            self.cloud.observe_zone(None, None)
            return
        value = round(statistics.median(self.recent))
        zone = self.zone_of(value)
        labels = {
            'GREEN': 'GREEN — dry / normal',
            'BLUE': 'BLUE — water detected',
            'BLUE_BLINK': 'BLUE BLINKING — elevated reading',
            'RED': 'RED — high reading',
            'RED_BLINK': 'RED BLINKING — calibrated critical range',
        }
        self.alert_var.set(labels[zone] + f'  (median {value})')
        self.cloud.state = {                       # plain dict; read by worker threads
            'zone': zone, 'median_adc': value, 'live_adc': self.current,
            'peak_adc': self.max_seen, 'calibrated_max': self.reference,
            'thresholds': {'blue': self.thresholds[0], 'blue_blink': self.thresholds[1],
                           'red': self.thresholds[2], 'red_blink': self.thresholds[3]},
        }
        if not self.calibrating:
            self.cloud.observe_zone(zone, value)

    def show_ai(self, r):
        if 'error' in r:
            self.ai_var.set('AI analyst: ' + r['error'])
            return
        self.ai_var.set(f"{r.get('status', '')}   |   trend: {r.get('trend', '?')}\n"
                        f"{r.get('explanation', '')}\nNext: {r.get('recommended_action', '')}")

    def toggle_mute(self):
        muted = self.cloud.toggle_mute()
        self.mute_btn.config(text='UNMUTE VOICE' if muted else 'MUTE VOICE')

    def close(self):

        self.cloud.stop()
        self.disconnect()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    SafeThresherApp(root)
    root.mainloop()

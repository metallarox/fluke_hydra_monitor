#!/usr/bin/env python3
"""Fluke Hydra 2620A/2625A - DMM with analog meter and chart over RS-232 (Python/Tkinter).

https://github.com/metallarox/fluke_hydra_monitor  ·  IV3HGA
"""
import csv
import datetime as dt
import math
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    import matplotlib.dates as mdates
    from matplotlib.ticker import MaxNLocator
except ImportError:
    raise SystemExit('Install Matplotlib: sudo apt install python3-matplotlib')

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    raise SystemExit('Install pyserial: sudo apt install python3-serial')

RANGES = {
    'V DC': [('AUTO', 'AUTO'), ('300 mV', '1'), ('3 V', '2'), ('30 V', '3'), ('150/300 V', '4')],
    'V AC': [('AUTO', 'AUTO'), ('300 mV', '1'), ('3 V', '2'), ('30 V', '3'), ('150/300 V', '4')],
    'Ohm': [('AUTO', 'AUTO'), ('300 Ω', '1'), ('3 kΩ', '2'), ('30 kΩ', '3'),
            ('300 kΩ', '4'), ('3 MΩ', '5'), ('10 MΩ', '6')],
    'Frequency': [('AUTO', 'AUTO'), ('900 Hz', '1'), ('9 kHz', '2'), ('90 kHz', '3'),
                  ('900 kHz', '4'), ('1 MHz', '5')],
    'Thermocouple': [(x, x) for x in 'JKETNRSBC'],
    'RTD Pt100': [('Pt100', 'PT')],
    'OFF': [('---', '')],
}
FUNCS = {'V DC': 'VDC', 'V AC': 'VAC', 'Ohm': 'OHMS', 'Frequency': 'FREQ',
         'Thermocouple': 'TEMP', 'RTD Pt100': 'TEMP', 'OFF': 'OFF'}
UNITS = {'V DC': 'V', 'V AC': 'V', 'Ohm': 'Ω', 'Frequency': 'Hz',
         'Thermocouple': '°C/°F', 'RTD Pt100': '°C/°F', 'OFF': ''}
SI_MODES = ('V DC', 'V AC', 'Ohm', 'Frequency')   # modes where an m/k/M prefix makes sense

LIVE, MONITOR, TRIGGER = 'LIVE', 'MONITOR (chart)', 'Trigger + LAST?'
NUM_RE = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?', re.I)
OVERLOAD = 9e9      # the Hydra reports overrange as a huge value (+9E+9): check on your unit
FRONT_LABEL = '0 · Front'   # CH0 = front-panel input jacks
CHANNELS = [FRONT_LABEL] + [str(i) for i in range(1, 21)]
ASSETS = Path(__file__).resolve().parent / 'assets'
__version__ = '1.0.0'
MAX_RETRY = 3       # consecutive failed reads tolerated before stopping


def ch_label(ch):
    """'CH00 · FRONT' for the front-panel input, 'CH07' for the rear channels."""
    return 'CH00 · FRONT' if ch == 0 else f'CH{ch:02d}'


def build_func(mode, range_label, terminals, ch):
    """Build the FUNC command for the selected configuration."""
    if mode == 'OFF':
        return f'FUNC {ch},OFF'
    code = dict(RANGES[mode])[range_label]
    if mode in ('Ohm', 'RTD Pt100'):
        return f'FUNC {ch},{FUNCS[mode]},{code},{terminals}'
    return f'FUNC {ch},{FUNCS[mode]},{code}'


def fmt_value(val, mode, digits=6):
    """Value formatted with an SI prefix (1.23456 mV instead of +0.00123456)."""
    if val is None or math.isnan(val):
        return 'OL', UNITS.get(mode, '')
    unit = UNITS.get(mode, '')
    if mode not in SI_MODES or val == 0:
        return f'{val:+.{digits}g}', unit
    exp = int(math.floor(math.log10(abs(val)) / 3) * 3)
    exp = max(-9, min(9, exp))
    scaled = val / 10 ** exp
    decimals = max(0, digits - 1 - int(math.floor(math.log10(abs(scaled)))))
    prefix = {-9: 'n', -6: 'µ', -3: 'm', 0: '', 3: 'k', 6: 'M', 9: 'G'}[exp]
    return f'{scaled:+.{decimals}f}', prefix + unit


class Instrument:
    def __init__(self, port):
        self.ser = serial.Serial(port=port, baudrate=9600, bytesize=8,
                                 parity='N', stopbits=1, timeout=0.10,
                                 xonxoff=False, rtscts=False, dsrdtr=False)
        self.ser.write(b'\x03')          # Ctrl-C: cancel any pending command
        self.ser.flush()
        time.sleep(.3)
        self.ser.reset_input_buffer()

    def command(self, cmd, timeout=3.5):
        self.ser.reset_input_buffer()
        self.ser.write((cmd + '\r').encode('ascii'))
        self.ser.flush()
        buf = bytearray()
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            buf.extend(self.ser.read(256))
            if buf.rstrip().endswith((b'=>', b'?>', b'!>')):
                break
        else:
            raise TimeoutError(f'No response to {cmd}')
        raw = buf.decode('ascii', errors='replace').replace('\r', '').split('\n')
        lines = [line.strip() for line in raw if line.strip()]
        prompt = lines[-1]
        if prompt.endswith('?>'):
            raise RuntimeError(f'{cmd}: syntax error (?>)')
        if prompt.endswith('!>'):
            raise RuntimeError(f'{cmd}: execution error (!>)')
        # ECHO ON: strip the command echo, if any.
        payload = [line for line in lines[:-1] if line.upper() != cmd.upper()]
        return '\n'.join(payload)

    def read_value(self, cmd, timeout=8):
        """Run a query and return (value, raw string); value is NaN on overrange."""
        raw = self.command(cmd, timeout=timeout)
        match = NUM_RE.search(raw)
        if not match:
            if 'OL' in raw.upper():
                return math.nan, raw
            raise ValueError(f'Non-numeric reading: {raw!r}')
        val = float(match.group())
        return (math.nan if abs(val) >= OVERLOAD else val), raw

    def close(self):
        self.ser.close()



# =====================================================================  theme

BG = '#0e1014'
PANEL = '#161920'
PANEL2 = '#1e222b'
BORDER = '#2a2f3a'
TEXT = '#e7e9ee'
MUTED = '#8b93a1'
ACCENT = '#ffc20e'        # Fluke yellow
ACCENT_HI = '#ffd54a'
GREEN = '#3ddc84'
RED = '#ff5252'
PLOT_BG = '#11141a'

SI_PREFIX = {-9: 'n', -6: 'µ', -3: 'm', 0: '', 3: 'k', 6: 'M', 9: 'G'}
SI_PARSE = {'n': -9, 'u': -6, 'µ': -6, 'm': -3, 'k': 3, 'K': 3, 'M': 6, 'G': 9}
# Classic analog-meter ranges: 1-3-10 sequence
FS_STEPS = sorted(m * 10.0 ** e for e in range(-6, 10) for m in (1, 3))


def eng_exp(x):
    """Engineering exponent (multiple of 3) for x."""
    if not x or math.isnan(x):
        return 0
    return max(-9, min(9, int(math.floor(math.log10(abs(x)) / 3) * 3)))


def parse_si(text):
    """'300m' -> 0.3, '1.5k' -> 1500, '-2' -> -2.0"""
    s = text.strip().replace(',', '.')
    if s and s[-1] in SI_PARSE:
        return float(s[:-1]) * 10 ** SI_PARSE[s[-1]]
    return float(s)


def choose_fs(a):
    for fs in FS_STEPS:
        if a <= fs:
            return fs
    return FS_STEPS[-1]


def pick_font(*families):
    available = set(tkfont.families())
    for fam in families:
        if fam in available:
            return fam
    return families[-1]


def apply_theme(root):
    ui = pick_font('Inter', 'Cantarell', 'Noto Sans', 'DejaVu Sans', 'TkDefaultFont')
    mono = pick_font('JetBrains Mono', 'DejaVu Sans Mono', 'Liberation Mono', 'TkFixedFont')
    root.configure(bg=BG)
    st = ttk.Style(root)
    st.theme_use('clam')
    st.configure('.', background=PANEL, foreground=TEXT, fieldbackground=PANEL2,
                 bordercolor=BORDER, lightcolor=PANEL2, darkcolor=PANEL2,
                 troughcolor=PANEL2, focuscolor=ACCENT, font=(ui, 10),
                 selectbackground=ACCENT, selectforeground='#000')
    st.configure('Bg.TFrame', background=BG)
    st.configure('Card.TFrame', background=PANEL)
    st.configure('TLabel', background=PANEL)
    st.configure('Bg.TLabel', background=BG)
    st.configure('Muted.TLabel', foreground=MUTED)
    st.configure('Section.TLabel', foreground=MUTED, font=(ui, 9, 'bold'))
    st.configure('Title.TLabel', background=BG, font=(ui, 18, 'bold'))
    st.configure('Sub.TLabel', background=BG, foreground=MUTED)
    st.configure('Note.TLabel', foreground='#b08a3c', font=(ui, 8))
    st.configure('Value.TLabel', foreground=ACCENT, font=(mono, 54, 'bold'))
    st.configure('ValueOL.TLabel', foreground=RED, font=(mono, 54, 'bold'))
    st.configure('Unit.TLabel', foreground=TEXT, font=(ui, 22))
    st.configure('Chip.TLabel', background=PANEL2, foreground=TEXT, padding=(9, 3), font=(ui, 9, 'bold'))
    st.configure('Rec.TLabel', background='#3a1416', foreground=RED, padding=(9, 3), font=(ui, 9, 'bold'))
    st.configure('Stats.TLabel', foreground=MUTED, font=(mono, 10))
    st.configure('Status.TLabel', background=BG, foreground=MUTED, font=(ui, 9))

    st.configure('TButton', background=PANEL2, foreground=TEXT, borderwidth=0,
                 padding=(12, 7), relief='flat')
    st.map('TButton', background=[('pressed', BORDER), ('active', '#272c37')])
    st.configure('Accent.TButton', background=ACCENT, foreground='#111', font=(ui, 10, 'bold'))
    st.map('Accent.TButton', background=[('pressed', '#e0a800'), ('active', ACCENT_HI)])
    st.configure('Stop.TButton', background=RED, foreground='#111', font=(ui, 10, 'bold'))
    st.map('Stop.TButton', background=[('pressed', '#d43c3c'), ('active', '#ff7373')])
    st.configure('Icon.TButton', padding=(8, 5))

    st.configure('TCombobox', arrowcolor=MUTED, padding=5, borderwidth=1)
    st.map('TCombobox', fieldbackground=[('readonly', PANEL2), ('disabled', PANEL)],
           foreground=[('disabled', '#555b66')], selectbackground=[('readonly', PANEL2)],
           selectforeground=[('readonly', TEXT)], bordercolor=[('focus', ACCENT)])
    st.configure('TEntry', padding=5, insertcolor=TEXT)
    st.map('TEntry', bordercolor=[('focus', ACCENT)], fieldbackground=[('disabled', PANEL)],
           foreground=[('disabled', '#555b66')])
    st.configure('TCheckbutton', indicatorbackground=PANEL2, indicatorforeground=ACCENT,
                 indicatorrelief='flat', padding=2)
    st.map('TCheckbutton', background=[('active', PANEL)],
           indicatorbackground=[('selected', ACCENT), ('active', BORDER)],
           indicatorforeground=[('selected', '#111')])
    st.configure('TSeparator', background=BORDER)
    root.option_add('*TCombobox*Listbox.background', PANEL2)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', ACCENT)
    root.option_add('*TCombobox*Listbox.selectForeground', '#111')
    root.option_add('*TCombobox*Listbox.font', (ui, 10))
    return ui, mono


# =====================================================================  gauge
class AnalogGauge(tk.Canvas):
    """Panel-style moving-coil meter with a damped needle."""
    FACE = '#efe7d2'
    FACE_EDGE = '#d9cfb4'
    INK = '#1c1a17'
    SCALE_RED = '#c0392b'
    BEZEL = '#08090b'
    A0, A1 = 140.0, 40.0          # scale start/end angles (degrees)
    PEG = 0.055                   # mechanical over-travel, fraction of full scale

    def __init__(self, master, mono, ui, **kw):
        super().__init__(master, highlightthickness=0, bd=0, bg=PANEL, **kw)
        self.mono, self.ui = mono, ui
        self.fs, self.bipolar, self.mode, self.caption = 1.0, False, 'V DC', ''
        self.pos = self.vel = self.target = 0.0
        self.overload = False
        self.geom = None
        self._anim = None
        self.bind('<Configure>', lambda e: self._draw_static())

    # --- API
    def set_scale(self, fs, bipolar, mode, caption=''):
        if (fs, bipolar, mode, caption) != (self.fs, self.bipolar, self.mode, self.caption):
            self.fs, self.bipolar, self.mode, self.caption = fs, bipolar, mode, caption
            self._draw_static()

    def set_value(self, v):
        self.overload = v is None or math.isnan(v)
        if self.overload:
            f = 1 + self.PEG
        elif self.bipolar:
            f = (v + self.fs) / (2 * self.fs)
        else:
            f = v / self.fs
        self.target = max(-self.PEG, min(1 + self.PEG, f))
        self.itemconfigure('ol', state='normal' if self.overload else 'hidden')
        if self._anim is None:
            self._anim = self.after(16, self._tick)

    def reset(self):
        self.target = 0.5 if self.bipolar else 0.0
        self.overload = False
        self.itemconfigure('ol', state='hidden')
        if self._anim is None:
            self._anim = self.after(16, self._tick)

    # --- drawing
    def _angle(self, f):
        return math.radians(self.A0 + (self.A1 - self.A0) * f)

    def _pt(self, f, r):
        cx, py, _ = self.geom
        a = self._angle(f)
        return cx + r * math.cos(a), py - r * math.sin(a)

    def _rounded(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.create_polygon(pts, smooth=True, **kw)

    def _ticks(self):
        exp = eng_exp(self.fs)
        d = self.fs / 10 ** exp                 # full scale in displayed units: 1, 3, 10, 30...
        mant = round(d / 10 ** math.floor(math.log10(d)))
        major = d / 5 if mant == 1 else d / 6
        minor = major / (4 if mant == 1 else 5)
        lo = -d if self.bipolar else 0.0
        span = d - lo
        n = int(round(span / minor))
        per_major = int(round(major / minor))
        label_every = 2 if self.bipolar else 1
        out = []
        for i in range(n + 1):
            val = lo + i * minor
            f = (val - lo) / span
            is_major = i % per_major == 0
            is_half = not is_major and per_major % 2 == 0 and i % (per_major // 2) == 0
            lbl = None
            if is_major and (i // per_major) % label_every == 0:
                lbl = '0' if abs(val) < 1e-12 else f'{val:.3g}'
            out.append((f, 'major' if is_major else 'half' if is_half else 'minor', lbl))
        return out, SI_PREFIX[exp]

    def _draw_static(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 80:
            return
        m = 8
        x0, y0, x1, y1 = m, m, w - m, h - m
        self._rounded(x0 - 2, y0 - 2, x1 + 2, y1 + 2, 18, fill=self.BEZEL, outline='')
        self._rounded(x0 + 6, y0 + 6, x1 - 6, y1 - 6, 12, fill=self.FACE, outline=self.FACE_EDGE, width=2)
        cx = w / 2
        py = y1 - 22
        r_w = ((x1 - x0) / 2 - 46) / math.sin(math.radians(50))
        r_h = py - y0 - 58
        R = max(40.0, min(r_w, r_h))
        self.geom = (cx, py, R)
        bbox = lambda r: (cx - r, py - r, cx + r, py + r)
        ext = self.A0 - self.A1

        # anti-parallax mirror
        self.create_arc(*bbox(R - 13), start=self.A1, extent=ext, style='arc',
                        outline='#b9c3cc', width=7)
        self.create_arc(*bbox(R - 13), start=self.A1, extent=ext, style='arc',
                        outline='#dfe6ec', width=2)
        # scale arc and red over-range zone
        self.create_arc(*bbox(R), start=self.A1, extent=ext, style='arc', outline=self.INK, width=2)
        peg = ext * self.PEG
        self.create_arc(*bbox(R + 3), start=self.A1 - peg, extent=peg, style='arc',
                        outline=self.SCALE_RED, width=6)
        if self.bipolar:
            self.create_arc(*bbox(R + 3), start=self.A0, extent=peg, style='arc',
                            outline=self.SCALE_RED, width=6)

        ticks, prefix = self._ticks()
        fsize = max(8, int(R / 17))
        for f, kind, lbl in ticks:
            length = {'major': 15, 'half': 10, 'minor': 6}[kind]
            width = 2 if kind == 'major' else 1
            self.create_line(*self._pt(f, R), *self._pt(f, R + length), fill=self.INK, width=width)
            if lbl is not None:
                self.create_text(*self._pt(f, R + length + fsize + 2), text=lbl,
                                 fill=self.INK, font=(self.ui, fsize, 'bold'))

        unit = UNITS.get(self.mode, '')
        if self.mode in SI_MODES:
            unit = prefix + unit
        sym = {'V DC': '⎓', 'V AC': '∿'}.get(self.mode, '')
        uy = py - R * 0.42
        self.create_text(cx, uy, text=unit, fill=self.INK, font=(self.ui, max(12, int(R / 7)), 'bold'))
        if sym:
            self.create_text(cx, uy + R * 0.14, text=sym, fill=self.INK, font=(self.ui, max(10, int(R / 10))))
        fs_txt = ' '.join(fmt_value(self.fs, self.mode, 3)).lstrip('+')
        rng = f'±{fs_txt}' if self.bipolar else f'0 … {fs_txt}'
        self.create_text(x0 + 22, y0 + 22, text=rng, anchor='nw', fill='#6d6655', font=(self.mono, 10, 'bold'))
        self.create_text(x1 - 22, y0 + 22, text=self.caption, anchor='ne', fill='#6d6655', font=(self.ui, 9))
        self.create_text(x0 + 22, y1 - 26, text='CL 1.5   ⊥', anchor='sw', fill='#8a826d', font=(self.ui, 9))
        # OL lamp
        self.create_oval(x1 - 58, y1 - 44, x1 - 40, y1 - 26, fill=RED, outline='#7a1d1d', tags='ol')
        self.create_text(x1 - 49, y1 - 52, text='OL', fill=self.SCALE_RED,
                         font=(self.ui, 9, 'bold'), anchor='s', tags='ol')
        self.itemconfigure('ol', state='normal' if self.overload else 'hidden')

        # needle (shadow + pointer + counterweight) and pivot cover
        self.create_line(0, 0, 0, 0, fill='#b7ae97', width=2, tags='shadow')
        self.create_line(0, 0, 0, 0, fill=self.INK, width=2, tags='needle')
        self.create_line(0, 0, 0, 0, fill=self.INK, width=6, capstyle='round', tags='needle_cw')
        pr = R * 0.17
        self.create_arc(cx - pr, py - pr, cx + pr, py + pr, start=0, extent=180,
                        fill='#202226', outline='#202226', style='pieslice')
        self.create_rectangle(x0 + 7, py, x1 - 7, y1 - 7, fill='#202226', outline='')
        self.create_oval(cx - 4, py - 10, cx + 4, py - 2, fill='#3a3d44', outline='')
        self._place_needle()

    def _place_needle(self):
        if not self.geom:
            return
        cx, py, R = self.geom
        tip = self._pt(self.pos, R + 6)
        a = self._angle(self.pos)
        tail = (cx - 16 * math.cos(a), py + 16 * math.sin(a))
        self.coords('needle', cx, py, *tip)
        self.coords('shadow', cx + 3, py + 3, tip[0] + 3, tip[1] + 4)
        self.coords('needle_cw', cx, py, *tail)

    def _tick(self):
        # 2nd-order moving coil: wn ≈ 13 rad/s, damping 0.55 (slight overshoot)
        wn, zeta, dt = 13.0, 0.55, 0.008
        for _ in range(2):
            acc = wn * wn * (self.target - self.pos) - 2 * zeta * wn * self.vel
            self.vel += acc * dt
            self.pos += self.vel * dt
            hi, lo = 1 + self.PEG + 0.01, -self.PEG - 0.01
            if self.pos > hi or self.pos < lo:
                self.pos = min(max(self.pos, lo), hi)
                self.vel *= -0.25            # bounce off the end stop
        self._place_needle()
        if abs(self.target - self.pos) < 3e-4 and abs(self.vel) < 3e-3:
            self.pos, self.vel = self.target, 0.0
            self._place_needle()
            self._anim = None
            return
        self._anim = self.after(16, self._tick)


# =====================================================================  app
class App:
    def __init__(self, root):
        self.root = root
        root.title('Fluke Hydra 2625A • RS-232 Multimeter')
        root.geometry('1240x820')
        root.minsize(1040, 680)
        self.ui, self.mono = apply_theme(root)
        self.icons = self._load_icons()
        self.events = queue.Queue()
        self.jobs = queue.Queue()
        self.connected = False
        self.running = False
        self.closing = False
        self.port = tk.StringVar(value='/dev/ttyUSB0')
        self.channel = tk.StringVar(value=FRONT_LABEL)
        self.mode = tk.StringVar(value='V DC')
        self.range = tk.StringVar(value='AUTO')
        self.terminals = tk.StringVar(value='2')
        self.period = tk.StringVar(value='1.0')
        self.acquisition = tk.StringVar(value=LIVE)
        self.window_seconds = tk.StringVar(value='60')
        self.autosave = tk.BooleanVar(value=False)
        self.y_auto = tk.BooleanVar(value=True)
        self.y_min = tk.StringVar(value='')
        self.y_max = tk.StringVar(value='')
        self.g_lock = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='Disconnected')
        self.ident = tk.StringVar(value='no instrument')
        self.value = tk.StringVar(value='— — —')
        self.unit = tk.StringVar(value='')
        self.chip_func = tk.StringVar(value='—')
        self.chip_ch = tk.StringVar(value='CH —')
        self.config = tk.StringVar(value='')
        self.stats = tk.StringVar(value='')
        self.history = deque(maxlen=50000)
        self.plot_dirty = False
        self.ylim = None                  # current Y limits (base units), with hysteresis
        self.log = []
        self.session = None               # (ch, mode) of the running measurement
        self.s_min = self.s_max = self.s_sum = None
        self.s_n = self.s_ol = 0
        self.g_fs = None
        self.g_down = 0
        self.g_neg = deque(maxlen=20)
        self.csv_file = self.csv_writer = None
        self.csv_path = ''
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()
        self._build()
        self._update_ranges()
        root.after(100, self._poll)
        root.after(300, self._refresh_plot)
        root.protocol('WM_DELETE_WINDOW', self._close)

    # ------------------------------------------------------------------ GUI
    def _load_icons(self):
        """Logo in the header and window icon; the app still runs if assets/ is missing."""
        icons = {}
        try:
            icons['header'] = tk.PhotoImage(file=str(ASSETS / 'logo_48.png'))
            icons['win'] = [tk.PhotoImage(file=str(ASSETS / f'logo_{n}.png')) for n in (128, 64, 32)]
            self.root.iconphoto(True, *icons['win'])
        except tk.TclError:
            pass
        return icons

    def _section(self, parent, title):
        ttk.Label(parent, text=title.upper(), style='Section.TLabel').pack(anchor='w', pady=(14, 6))
        box = ttk.Frame(parent, style='Card.TFrame')
        box.pack(fill='x')
        return box

    def _field(self, box, row, label, widget):
        ttk.Label(box, text=label, style='Muted.TLabel').grid(row=row, column=0, sticky='w', pady=4, padx=(0, 10))
        widget.grid(row=row, column=1, sticky='ew', pady=4)

    def _build(self):
        shell = ttk.Frame(self.root, style='Bg.TFrame', padding=14)
        shell.pack(fill='both', expand=True)

        # --- header
        head = ttk.Frame(shell, style='Bg.TFrame')
        head.pack(fill='x', pady=(0, 12))
        if 'header' in self.icons:
            ttk.Label(head, image=self.icons['header'], style='Bg.TLabel').pack(side='left', padx=(0, 12))
        ttk.Label(head, text='HYDRA 2625A', style='Title.TLabel').pack(side='left')
        ttk.Label(head, text=f'  Fluke data acquisition · RS-232 · v{__version__} · IV3HGA', style='Sub.TLabel').pack(side='left', pady=(6, 0))
        right = ttk.Frame(head, style='Bg.TFrame')
        right.pack(side='right')
        self.led = tk.Canvas(right, width=14, height=14, bg=BG, highlightthickness=0)
        self.led.create_oval(2, 2, 12, 12, fill='#3a3f4a', outline='', tags='dot')
        self.led.pack(side='left', padx=(0, 7))
        ttk.Label(right, textvariable=self.ident, style='Sub.TLabel').pack(side='left')

        body = ttk.Frame(shell, style='Bg.TFrame')
        body.pack(fill='both', expand=True)

        # --- sidebar
        side = ttk.Frame(body, style='Card.TFrame', padding=(16, 4, 16, 16), width=300)
        side.pack(side='left', fill='y', padx=(0, 12))
        side.pack_propagate(False)

        box = self._section(side, 'Connection')
        box.columnconfigure(0, weight=1)
        self.ports = ttk.Combobox(box, textvariable=self.port)
        self.ports.grid(row=0, column=0, sticky='ew')
        ttk.Button(box, text='⟳', width=3, style='Icon.TButton',
                   command=self._refresh_ports).grid(row=0, column=1, padx=(6, 0))
        self.connect_btn = ttk.Button(box, text='Connect', style='Accent.TButton', command=self._connect)
        self.connect_btn.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        self._refresh_ports()

        box = self._section(side, 'Channel')
        box.columnconfigure(1, weight=1)
        self._field(box, 0, 'Channel', ttk.Combobox(box, textvariable=self.channel, width=8,
                    values=CHANNELS, state='readonly'))
        self.mode_cb = ttk.Combobox(box, textvariable=self.mode, values=list(RANGES), state='readonly', width=14)
        self.mode_cb.bind('<<ComboboxSelected>>', lambda e: self._update_ranges())
        self._field(box, 1, 'Function', self.mode_cb)
        self.range_cb = ttk.Combobox(box, textvariable=self.range, state='readonly', width=14)
        self._field(box, 2, 'Range', self.range_cb)
        self.term_cb = ttk.Combobox(box, textvariable=self.terminals, values=['2', '4'], state='readonly', width=8)
        self._field(box, 3, 'Ω/RTD wires', self.term_cb)
        ttk.Button(box, text='Apply configuration', command=self._apply).grid(
            row=4, column=0, columnspan=2, sticky='ew', pady=(8, 0))

        box = self._section(side, 'Acquisition')
        box.columnconfigure(1, weight=1)
        self.acq_cb = ttk.Combobox(box, textvariable=self.acquisition, values=[LIVE, MONITOR, TRIGGER],
                                   state='readonly', width=14)
        self.acq_cb.bind('<<ComboboxSelected>>', self._mode_changed)
        self._field(box, 0, 'Mode', self.acq_cb)
        self._field(box, 1, 'Period (s)', ttk.Entry(box, textvariable=self.period, width=8))
        ttk.Checkbutton(box, text='Record to CSV while measuring', variable=self.autosave).grid(
            row=2, column=0, columnspan=2, sticky='w', pady=(6, 0))
        self.start_btn = ttk.Button(box, text='▶  Start', style='Accent.TButton', command=self._toggle_run)
        self.start_btn.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(10, 0))
        ttk.Button(box, text='Export CSV…', command=self._export).grid(
            row=4, column=0, columnspan=2, sticky='ew', pady=(6, 0))

        ttk.Label(side, style='Note.TLabel', wraplength=262, justify='left',
                  text='CH0 is the front-panel input. 4-wire only on CH1–CH10 (CH n+10 is disabled). '
                       'Changing function clears the Fluke alarm limits, Mx+B scaling and Review array. '
                       '"Start" applies the configuration automatically if it changed.').pack(side='bottom', anchor='w')

        # --- main area
        main = ttk.Frame(body, style='Bg.TFrame')
        main.pack(side='left', fill='both', expand=True)

        card = ttk.Frame(main, style='Card.TFrame', padding=(22, 14))
        card.pack(fill='x')
        chips = ttk.Frame(card, style='Card.TFrame')
        chips.pack(fill='x')
        ttk.Label(chips, textvariable=self.chip_ch, style='Chip.TLabel').pack(side='left')
        ttk.Label(chips, textvariable=self.chip_func, style='Chip.TLabel').pack(side='left', padx=6)
        self.rec_chip = ttk.Label(chips, text='● REC', style='Rec.TLabel')
        ttk.Label(chips, textvariable=self.config, style='Muted.TLabel').pack(side='right')
        reading = ttk.Frame(card, style='Card.TFrame')
        reading.pack(fill='x', pady=(4, 0))
        self.value_lbl = ttk.Label(reading, textvariable=self.value, style='Value.TLabel')
        self.value_lbl.pack(side='left')
        ttk.Label(reading, textvariable=self.unit, style='Unit.TLabel').pack(side='left', padx=(14, 0), pady=(22, 0))
        ttk.Label(card, textvariable=self.stats, style='Stats.TLabel').pack(anchor='w')

        self.view = ttk.Frame(main, style='Bg.TFrame')
        self.view.pack(fill='both', expand=True, pady=(12, 0))

        # analog meter (LIVE / Trigger)
        self.gauge_box = ttk.Frame(self.view, style='Card.TFrame', padding=14)
        gbar = ttk.Frame(self.gauge_box, style='Card.TFrame')
        gbar.pack(fill='x', pady=(0, 8))
        ttk.Label(gbar, text='ANALOG METER', style='Section.TLabel').pack(side='left')
        ttk.Checkbutton(gbar, text='Lock range', variable=self.g_lock).pack(side='right')
        self.gauge = AnalogGauge(self.gauge_box, self.mono, self.ui)
        self.gauge.pack(fill='both', expand=True)

        # chart (MONITOR)
        self.graph_box = ttk.Frame(self.view, style='Card.TFrame', padding=14)
        gbar = ttk.Frame(self.graph_box, style='Card.TFrame')
        gbar.pack(fill='x', pady=(0, 6))
        ttk.Label(gbar, text='HISTORY', style='Section.TLabel').pack(side='left', padx=(0, 12))
        ttk.Label(gbar, text='Window', style='Muted.TLabel').pack(side='left')
        ttk.Combobox(gbar, textvariable=self.window_seconds, width=6, state='readonly',
                     values=['10', '30', '60', '120', '300', '600', '1800', '3600']).pack(side='left', padx=(6, 4))
        ttk.Label(gbar, text='s', style='Muted.TLabel').pack(side='left', padx=(0, 16))
        ttk.Checkbutton(gbar, text='Autoscale Y', variable=self.y_auto,
                        command=self._yauto_changed).pack(side='left', padx=(0, 10))
        self.ymin_e = ttk.Entry(gbar, textvariable=self.y_min, width=8)
        self.ymax_e = ttk.Entry(gbar, textvariable=self.y_max, width=8)
        ttk.Label(gbar, text='min', style='Muted.TLabel').pack(side='left')
        self.ymin_e.pack(side='left', padx=(4, 8))
        ttk.Label(gbar, text='max', style='Muted.TLabel').pack(side='left')
        self.ymax_e.pack(side='left', padx=(4, 0))
        for e in (self.ymin_e, self.ymax_e):
            e.bind('<Return>', lambda ev: self._redraw_plot())
            e.bind('<FocusOut>', lambda ev: self._redraw_plot())
        ttk.Button(gbar, text='Clear', command=self._clear_graph).pack(side='right')

        self.fig = Figure(figsize=(8, 3), dpi=100, facecolor=PANEL)
        self.ax = self.fig.add_subplot(111, facecolor=PLOT_BG)
        for sp in self.ax.spines.values():
            sp.set_color(BORDER)
        self.ax.tick_params(colors=MUTED, labelsize=9)
        self.ax.grid(True, color='#252a34', linewidth=.8)
        self.ax.yaxis.label.set_color(MUTED)
        self.line, = self.ax.plot([], [], color=ACCENT, linewidth=1.6)
        self.last_dot, = self.ax.plot([], [], 'o', color=ACCENT, markersize=6,
                                      markeredgecolor=PLOT_BG, markeredgewidth=1.5)
        self.mean_line = self.ax.axhline(0, color=MUTED, linewidth=.8, linestyle='--', alpha=0)
        self.fill = None
        locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
        locator.intervald[mdates.SECONDLY] = [1, 2, 5, 10, 15, 30]
        locator.intervald[mdates.MINUTELY] = [1, 2, 5, 10, 15, 30]
        self.ax.xaxis.set_major_locator(locator)
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
        self.fig.subplots_adjust(left=.08, right=.985, top=.97, bottom=.12)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.graph_box)
        self.canvas.get_tk_widget().configure(bg=PANEL, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill='both', expand=True)

        ttk.Label(main, textvariable=self.status, style='Status.TLabel').pack(anchor='w', pady=(8, 0))
        self._yauto_changed()
        self._mode_changed()

    def _set_led(self, color):
        self.led.itemconfigure('dot', fill=color)

    def _blink(self):
        self._set_led(ACCENT)
        self.root.after(120, lambda: self._set_led(GREEN if self.connected else '#3a3f4a'))

    def _mode_changed(self, event=None):
        if event is not None and self.running:
            self._stop()      # a new mode requires a new Start
        if self.acquisition.get() == MONITOR:
            self.gauge_box.pack_forget()
            self.graph_box.pack(fill='both', expand=True)
            self.plot_dirty = True
        else:
            self.graph_box.pack_forget()
            self.gauge_box.pack(fill='both', expand=True)

    def _yauto_changed(self):
        state = 'disabled' if self.y_auto.get() else 'normal'
        if not self.y_auto.get() and self.ylim and not self.y_min.get():
            exp = self._plot_exp()
            self.y_min.set(f'{self.ylim[0] / 10 ** exp:.4g}{SI_PREFIX[exp]}')
            self.y_max.set(f'{self.ylim[1] / 10 ** exp:.4g}{SI_PREFIX[exp]}')
        self.ymin_e.configure(state=state)
        self.ymax_e.configure(state=state)
        self.plot_dirty = True

    def _refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.ports['values'] = ports
        if ports and self.port.get() not in ports:
            self.port.set(ports[0])

    def _update_ranges(self):
        options = [name for name, _ in RANGES[self.mode.get()]]
        self.range_cb['values'] = options
        self.range.set(options[0])
        self.term_cb['state'] = 'readonly' if self.mode.get() in ('Ohm', 'RTD Pt100') else 'disabled'

    # ---------------------------------------------------------------- gauge
    def _update_gauge(self, val, mode, ch):
        caption = ch_label(ch)
        if math.isnan(val):
            self.gauge.set_value(None)
            return
        if not self.g_lock.get():
            a = abs(val)
            need = choose_fs(a)
            if self.g_fs is None or need > self.g_fs:
                self.g_fs, self.g_down = need, 0            # range up immediately
            elif choose_fs(a / 0.9) < self.g_fs:
                self.g_down += 1                            # range down only after 4 readings
                if self.g_down >= 4:
                    self.g_fs, self.g_down = choose_fs(a / 0.9), 0
            else:
                self.g_down = 0
            self.g_neg.append(val < 0)
        bipolar = mode in ('V DC', 'Thermocouple', 'RTD Pt100') and any(self.g_neg)
        self.gauge.set_scale(self.g_fs or 1.0, bipolar, mode, caption)
        self.gauge.set_value(val)

    # ---------------------------------------------------------------- chart
    def _clear_graph(self):
        self.history.clear()
        self.ylim = None
        self._redraw_plot()

    def _refresh_plot(self):
        if self.closing:
            return
        # Redraw even without new points so the time axis keeps scrolling.
        if self.acquisition.get() == MONITOR and (self.plot_dirty or self.running):
            self._redraw_plot()
        self.root.after(300, self._refresh_plot)

    def _plot_mode(self):
        return self.session[1] if self.session else self.mode.get()

    def _plot_exp(self):
        if self._plot_mode() not in SI_MODES or not self.ylim:
            return 0
        return eng_exp(max(abs(self.ylim[0]), abs(self.ylim[1])))

    def _auto_ylim(self, lo, hi):
        """Nice limits with hysteresis: expand at once, shrink only when really needed."""
        span = hi - lo
        pad = max(span * .08, max(abs(lo), abs(hi)) * 0.002, 1e-9)
        if self.ylim:
            cur_lo, cur_hi = self.ylim
            inside = cur_lo <= lo and hi <= cur_hi
            if inside and (span + 2 * pad) > 0.35 * (cur_hi - cur_lo):
                return self.ylim
        ticks = MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]).tick_values(lo - pad, hi + pad)
        self.ylim = (float(ticks[0]), float(ticks[-1]))
        return self.ylim

    def _redraw_plot(self):
        self.plot_dirty = False
        try:
            span = float(self.window_seconds.get())
        except ValueError:
            span = 60
        now = dt.datetime.now()
        cutoff = now - dt.timedelta(seconds=span)
        visible = [(t, v) for t, v in self.history if t >= cutoff]
        finite = [v for _, v in visible if not math.isnan(v)]

        if self.y_auto.get():
            if finite:
                self._auto_ylim(min(finite), max(finite))
            elif not self.ylim:
                self.ylim = (-1.0, 1.0)
        else:
            try:
                lo, hi = parse_si(self.y_min.get()), parse_si(self.y_max.get())
                if hi > lo:
                    self.ylim = (lo, hi)
            except ValueError:
                pass
            if not self.ylim:
                self.ylim = (-1.0, 1.0)

        exp = self._plot_exp()
        k = 10.0 ** exp
        mode = self._plot_mode()
        unit = UNITS.get(mode) or 'Value'
        if mode in SI_MODES:
            unit = SI_PREFIX[exp] + unit

        if self.fill is not None:
            self.fill.remove()
            self.fill = None
        if visible:
            ts = mdates.date2num([t for t, _ in visible])
            vs = [v / k for _, v in visible]               # NaN (OL) breaks the line
            self.line.set_data(ts, vs)
            base = self.ylim[0] / k
            self.fill = self.ax.fill_between(ts, vs, base, color=ACCENT, alpha=.08, linewidth=0)
            last_t, last_v = visible[-1]
            if not math.isnan(last_v):
                self.last_dot.set_data([mdates.date2num(last_t)], [last_v / k])
            else:
                self.last_dot.set_data([], [])
        else:
            self.line.set_data([], [])
            self.last_dot.set_data([], [])
        if finite:
            self.mean_line.set_ydata([sum(finite) / len(finite) / k] * 2)
            self.mean_line.set_alpha(.7)
        else:
            self.mean_line.set_alpha(0)
        self.ax.set_ylim(self.ylim[0] / k, self.ylim[1] / k)
        self.ax.set_xlim(mdates.date2num(cutoff), mdates.date2num(now + dt.timedelta(seconds=.1)))
        self.ax.set_ylabel(unit)
        self.canvas.draw_idle()

    # ---------------------------------------------------------------- actions
    def _enqueue(self, action, **kw):
        self.jobs.put((action, kw))

    def _connect(self):
        if self.connected:
            self._stop()
            self._enqueue('disconnect')
        else:
            self.status.set('Connecting…')
            self._enqueue('connect', port=self.port.get())

    def _current_config(self):
        """(channel, FUNC command) from the GUI, or None if invalid."""
        mode = self.mode.get()
        ch = int(self.channel.get().split()[0])
        if mode in ('Ohm', 'RTD Pt100') and self.terminals.get() == '4' and not (1 <= ch <= 10):
            messagebox.showerror('Configuration', '4-wire measurement is only available on channels 1–10.')
            return None
        return ch, build_func(mode, self.range.get(), self.terminals.get(), ch)

    def _apply(self):
        if not self.connected:
            messagebox.showwarning('Connection', 'Connect the Fluke first.')
            return
        cfg = self._current_config()
        if cfg:
            self._stop()
            self._enqueue('configure', channel=cfg[0], command=cfg[1])

    def _toggle_run(self):
        self._stop() if self.running else self._start()

    def _set_running(self, on):
        self.running = on
        self.start_btn.configure(text='■  Stop' if on else '▶  Start',
                                 style='Stop.TButton' if on else 'Accent.TButton')

    def _start(self):
        if not self.connected:
            messagebox.showwarning('Connection', 'Connect the Fluke first.')
            return
        try:
            seconds = float(self.period.get().replace(',', '.'))
            if not 0.5 <= seconds <= 3600:
                raise ValueError()
        except ValueError:
            messagebox.showerror('Period', 'Enter a period between 0.5 and 3600 seconds.')
            return
        mode = self.mode.get()
        if mode == 'OFF':
            messagebox.showwarning('Function', 'Select a measurement function.')
            return
        cfg = self._current_config()
        if not cfg:
            return
        self._close_csv()
        if self.autosave.get():
            stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
            path = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('CSV', '*.csv')],
                                                initialfile=f"hydra_CH{cfg[0]:02d}{'_front' if cfg[0] == 0 else ''}_{stamp}.csv")
            if not path:
                return
            self.csv_file = open(path, 'w', newline='', encoding='utf-8')
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(['timestamp', 'channel', 'function', 'value', 'unit'])
            self.csv_path = path
            self.rec_chip.pack(side='left')
        self._set_running(True)
        self.session = (cfg[0], mode)
        self.chip_ch.set(ch_label(cfg[0]))
        self.chip_func.set(f'{mode} · {self.range.get()}')
        self.history.clear()
        self.ylim = None
        self.s_min = self.s_max = self.s_sum = None
        self.s_n = self.s_ol = 0
        self.stats.set('')
        self.g_fs, self.g_down = None, 0
        self.g_neg.clear()
        self.plot_dirty = True
        self._enqueue('start', channel=cfg[0], command=cfg[1], period=seconds,
                      acq=self.acquisition.get(), mode=mode)

    def _stop(self):
        if self.running:
            self._enqueue('stop')
        self._set_running(False)
        self._close_csv()

    def _close_csv(self):
        if self.csv_file:
            self.csv_file.close()
            self.status.set(f'File closed: {self.csv_path}')
        self.csv_file = self.csv_writer = None
        self.rec_chip.pack_forget()

    # ---------------------------------------------------------------- worker
    def _worker(self):
        """The only thread that talks to the serial port: no concurrent access."""
        inst = None
        applied = None      # (channel, FUNC command) currently loaded in the instrument
        run = None          # parameters of the running measurement
        monitoring = False
        next_at = 0.0
        errors = 0

        def mon_off():
            nonlocal monitoring
            if monitoring and inst:
                try:
                    inst.command('MON 0')
                except Exception:
                    pass
            monitoring = False

        def release():
            nonlocal inst, applied, run
            run = None
            mon_off()
            if inst:
                try:
                    inst.close()
                except Exception:
                    pass
            inst = applied = None

        def configure(ch, cmd):
            nonlocal applied
            mon_off()
            applied = None
            inst.command('SCAN 0')
            inst.command(cmd)
            state = inst.command(f'FUNC? {ch}')
            applied = (ch, cmd)
            self.events.put(('configured', (ch, state)))

        while True:
            wait = 0.05
            if run:
                wait = min(wait, max(0.0, next_at - time.monotonic()))
            try:
                action, p = self.jobs.get(timeout=wait)
            except queue.Empty:
                action, p = None, {}
            try:
                if action == 'quit':
                    release()
                    return
                elif action == 'connect':
                    release()                       # inst=None even if opening fails
                    inst = Instrument(p['port'])
                    self.events.put(('connected', inst.command('*IDN?')))
                elif action == 'disconnect':
                    release()
                    self.events.put(('disconnected', None))
                elif action == 'stop':
                    run = None
                    mon_off()
                    self.events.put(('status', 'Measurement stopped'))
                elif action in ('configure', 'start'):
                    run = None
                    if inst is None:
                        raise RuntimeError('Instrument not connected')
                    if action == 'configure' or applied != (p['channel'], p['command']):
                        configure(p['channel'], p['command'])
                    if action == 'start':
                        mon_off()
                        if p['acq'] in (LIVE, MONITOR):
                            inst.command(f"MON 1,{p['channel']}")
                            monitoring = True
                        run, errors = p, 0
                        next_at = time.monotonic()
                        self.events.put(('status', f"{p['acq']} started on {ch_label(p['channel'])}"))

                if run and time.monotonic() >= next_at:
                    if run['acq'] in (LIVE, MONITOR):
                        # MONITOR is a continuous measurement run by the instrument itself.
                        val, raw = inst.read_value('MON_VAL?', timeout=2.5)
                    else:
                        inst.command('*TRG', timeout=8)
                        val, raw = inst.read_value(f"LAST? {run['channel']}")
                    errors = 0
                    stamp = dt.datetime.now()
                    self.events.put(('reading', (run['channel'], run['mode'], val, stamp)))
                    # Fixed-step scheduling: no cumulative period drift.
                    next_at += run['period']
                    if next_at < time.monotonic():
                        next_at = time.monotonic() + run['period']
            except Exception as e:
                if run and action is None and errors < MAX_RETRY:
                    # Single error while measuring: retry instead of stopping everything.
                    errors += 1
                    self.events.put(('status', f'Read failed ({errors}/{MAX_RETRY}): {e}'))
                    next_at = time.monotonic() + run['period']
                    continue
                run = None
                mon_off()
                self.events.put(('error', str(e)))

    # ---------------------------------------------------------------- events
    def _poll(self):
        try:
            while True:
                typ, info = self.events.get_nowait()
                if typ == 'connected':
                    self.connected = True
                    self.connect_btn.configure(text='Disconnect', style='TButton')
                    self.ident.set(info.replace(',', ' · ') or 'connected')
                    self.status.set(f'Connected on {self.port.get()}')
                    self._set_led(GREEN)
                elif typ == 'disconnected':
                    self.connected = False
                    self.connect_btn.configure(text='Connect', style='Accent.TButton')
                    self.ident.set('no instrument')
                    self.status.set('Disconnected')
                    self.value.set('— — —')
                    self.unit.set('')
                    self._set_led('#3a3f4a')
                    self.gauge.reset()
                elif typ == 'configured':
                    ch, state = info
                    self.config.set(f'Instrument: {ch_label(ch)} {state}')
                    self.status.set('Configuration applied')
                elif typ == 'reading':
                    self._on_reading(*info)
                elif typ == 'status':
                    self.status.set(info)
                elif typ == 'error':
                    self._set_running(False)
                    self._close_csv()
                    self.status.set(f'Error: {info}')
                    self._set_led(RED)
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(100, self._poll)

    def _on_reading(self, ch, mode, val, stamp):
        if not self.running:
            return                      # reading arrived after "Stop": discard it
        self._blink()
        text, unit = fmt_value(val, mode)
        self.value.set(text)
        self.value_lbl.configure(style='ValueOL.TLabel' if math.isnan(val) else 'Value.TLabel')
        self.unit.set(unit)
        ts = stamp.isoformat(timespec='milliseconds')
        row = (ts, ch, mode, '' if math.isnan(val) else val, UNITS[mode])
        self.log.append(row)
        if self.csv_writer:
            self.csv_writer.writerow(row)
            self.csv_file.flush()
        self.history.append((stamp, val))
        self.plot_dirty = True
        if self.acquisition.get() != MONITOR:
            self._update_gauge(val, mode, ch)
        if math.isnan(val):
            self.s_ol += 1
        else:
            self.s_n += 1
            self.s_min = val if self.s_min is None else min(self.s_min, val)
            self.s_max = val if self.s_max is None else max(self.s_max, val)
            self.s_sum = val if self.s_sum is None else self.s_sum + val
        if self.s_n:
            f = lambda v: ' '.join(fmt_value(v, mode))
            ol = f'   OL {self.s_ol}' if self.s_ol else ''
            self.stats.set(f'min {f(self.s_min)}   max {f(self.s_max)}   '
                           f'mean {f(self.s_sum / self.s_n)}   n {self.s_n}{ol}')
        self.status.set(f'Last reading {stamp:%H:%M:%S} · {len(self.log)} readings total')

    def _export(self):
        if not self.log:
            messagebox.showinfo('CSV', 'No readings to export yet.')
            return
        path = filedialog.asksaveasfilename(defaultextension='.csv',
                                            initialfile='fluke_2625a.csv', filetypes=[('CSV', '*.csv')])
        if path:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp', 'channel', 'function', 'value', 'unit'])
                writer.writerows(self.log)
            self.status.set(f'Saved {len(self.log)} readings to {path}')

    def _close(self):
        self.closing = True
        self._close_csv()
        self._enqueue('quit')
        self.worker.join(timeout=3)     # give the worker time to send MON 0
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
